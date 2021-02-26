# (C) Datadog, Inc. 2018-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

from __future__ import division

import math
import re

import requests
from six import iteritems
from six.moves.urllib.parse import quote, urljoin

from datadog_checks.base import AgentCheck
from datadog_checks.base.utils.headers import headers
from datadog_checks.couch import errors, metrics


class CouchDb(AgentCheck):
    HTTP_CONFIG_REMAPPER = {'user': {'name': 'username'}}
    TIMEOUT = 5
    COUCH_SERVICE_CHECK_NAME = 'couchdb.can_connect'
    SG_SERVICE_CHECK_NAME = 'couchdb.sync_gateway.can_connect'
    SOURCE_TYPE_NAME = 'couchdb'
    MAX_DB = 50

    def __init__(self, name, init_config, instances):
        super(CouchDb, self).__init__(name, init_config, instances)
        self.checker = None

    def get(self, url, service_check_tags, run_check=False, service_check_name=None):
        """Hit a given URL and return the parsed json"""
        self.log.debug('Fetching CouchDB stats at url: %s', url)

        if service_check_name is None:
            service_check_name = self.COUCH_SERVICE_CHECK_NAME

        # Override Accept request header so that failures are not redirected to the Futon web-ui
        request_headers = headers(self.agentConfig)
        request_headers['Accept'] = 'text/json'

        try:
            r = self.http.get(url, headers=request_headers)
            r.raise_for_status()
            if run_check:
                self.service_check(
                    service_check_name,
                    AgentCheck.OK,
                    tags=service_check_tags,
                    message='Connection to %s was successful' % url,
                )
        except requests.exceptions.Timeout as e:
            self.service_check(
                service_check_name,
                AgentCheck.CRITICAL,
                tags=service_check_tags,
                message="Request timeout: {0}, {1}".format(url, e),
            )
            raise
        except requests.exceptions.HTTPError as e:
            self.service_check(service_check_name, AgentCheck.CRITICAL, tags=service_check_tags, message=str(e))
            raise
        except Exception as e:
            self.service_check(service_check_name, AgentCheck.CRITICAL, tags=service_check_tags, message=str(e))
            raise
        return r.json()

    def check(self, instance):
        server = self.get_server(instance)
        sync_gateway_url = self.instance.get("sync_gateway_url")
        if self.checker is None:
            name = instance.get('name', server)
            tags = ["instance:{0}".format(name)] + self.get_config_tags(instance)

            try:
                version = self.get(self.get_server(instance), tags, True)['version']
                if version is not None:
                    self.set_metadata('version', version)
                else:
                    self.log.debug("Could not parse CouchDB version: %s", version)

            except Exception as e:
                raise errors.ConnectionError("Unable to talk to the server: {}".format(e))

            major_version = int(version.split('.')[0])
            if major_version == 0:
                raise errors.BadVersionError("Unknown version {}".format(version))
            elif major_version <= 1:
                self.checker = CouchDB1(self)
            else:
                # v2 of the CouchDB check supports versions 2 and higher of Couch
                self.checker = CouchDB2(self)

            if sync_gateway_url:
                url = urljoin(sync_gateway_url, '/_expvar')
                self.get_sync_gateway(url, tags)

        self.checker.check(instance)

    def get_server(self, instance):
        server = instance.get('server')
        if server is None:
            raise errors.BadConfigError("A server must be specified")
        return server

    def get_config_tags(self, instance):
        tags = instance.get('tags', [])

        # Clean up tags in case there was a None entry in the instance
        # e.g. if the yaml contains tags: but no actual tags
        return list(set(tags)) if tags else []

    def get_sync_gateway(self, url, tags):
        gateway_metrics = self.get(url, tags, service_check_name=self.SG_SERVICE_CHECK_NAME).get('syncgateway', {})
        global_resource_stats = gateway_metrics.get('global', {}).get('resource_utilization')
        for mname, mval in iteritems(global_resource_stats):
            try:
                self._submit_gateway_metrics(mname, mval, tags)
            except Exception as e:
                self.log.debug("Unable to parse metric %s with value `%s: %s`", mname, mval, str(e))

        per_db_stats = gateway_metrics.get('per_db', {})
        for db, db_groups in iteritems(per_db_stats):
            db_tags = ['db:{}'.format(db)] + tags
            for subgroup, db_metrics in iteritems(db_groups):
                self.log.debug("Submitting metrics for group `%s`: `%s`", subgroup, metrics)
                for mname, mval in iteritems(db_metrics):
                    try:
                        self._submit_gateway_metrics(mname, mval, db_tags, subgroup)
                    except Exception as e:
                        self.log.debug("Unable to parse metric %s with value `%s`: %s", mname, mval, str(e))

    def _submit_gateway_metrics(self, mname, mval, tags, prefix=None):
        namespace = '.'.join(['couchdb', 'sync_gateway'])
        if prefix:
            namespace = '.'.join([namespace, prefix])

        if prefix == 'database' and mname in ['cache_feed', 'import_feed']:
            # Handle cache_feed stats
            for cfname, cfval in iteritems(mval):
                self.gauge('.'.join([namespace, mname, cfname]), cfval, tags)
        elif prefix == 'gsi_views':
            # gsi view metrics are formatted with design doc and views `sync_gateway_2.1.access_query_count`
            # parse design doc as tag and submit rest as a metric
            match = re.match(r'\{([^}:;]+)\}-(\w+):', mname)
            if match:
                design_doc_tag = match.groups()[0]
                gsi_tags = ['design_doc_name:{}'.format(design_doc_tag)] + tags
                ddname = match.groups()[0]
                self.monotonic_count('.'.join([namespace, ddname]), tags=gsi_tags)

        elif mname in metrics.COUNT_METRICS:
            self.monotonic_count('.'.join([namespace, mname]), mval, tags)
        else:
            self.gauge('.'.join([namespace, mname]), mval, tags)


class CouchDB1:
    """Extracts stats from CouchDB via its REST API
    http://wiki.apache.org/couchdb/Runtime_Statistics
    """

    def __init__(self, agent_check):
        self.db_exclude = {}
        self.agent_check = agent_check
        self.gauge = agent_check.gauge

    def _create_metric(self, data, tags=None):
        overall_stats = data.get('stats', {})
        for key, stats in iteritems(overall_stats):
            for metric, val in iteritems(stats):
                if val['current'] is not None:
                    metric_name = '.'.join(['couchdb', key, metric])
                    self.gauge(metric_name, val['current'], tags=tags)

        for db_name, db_stats in iteritems(data.get('databases', {})):
            for name, val in iteritems(db_stats):
                if name in ['doc_count', 'disk_size'] and val is not None:
                    metric_name = '.'.join(['couchdb', 'by_db', name])
                    metric_tags = list(tags)
                    metric_tags.append('db:%s' % db_name)
                    self.gauge(metric_name, val, tags=metric_tags, device_name=db_name)

    def check(self, instance):
        server = self.agent_check.get_server(instance)
        tags = ['instance:%s' % server] + self.agent_check.get_config_tags(instance)
        data = self.get_data(server, instance, tags)
        self._create_metric(data, tags=tags)

    def get_data(self, server, instance, tags):
        # The dictionary to be returned.
        couchdb = {'stats': None, 'databases': {}}

        # First, get overall statistics.
        endpoint = '/_stats/'

        url = urljoin(server, endpoint)

        overall_stats = self.agent_check.get(url, tags, True)

        # No overall stats? bail out now
        if overall_stats is None:
            raise Exception("No stats could be retrieved from %s" % url)

        couchdb['stats'] = overall_stats

        # Next, get all database names.
        endpoint = '/_all_dbs/'

        url = urljoin(server, endpoint)

        # Get the list of included databases.
        db_include = instance.get('db_include', instance.get('db_whitelist'))
        db_include = set(db_include) if db_include else None
        self.db_exclude.setdefault(server, [])
        self.db_exclude[server].extend(instance.get('db_exclude', instance.get('db_blacklist', [])))
        databases = set(self.agent_check.get(url, tags)) - set(self.db_exclude[server])
        databases = databases.intersection(db_include) if db_include else databases

        max_dbs_per_check = instance.get('max_dbs_per_check', self.agent_check.MAX_DB)
        if len(databases) > max_dbs_per_check:
            self.agent_check.warning('Too many databases, only the first %s will be checked.', max_dbs_per_check)
            databases = list(databases)[:max_dbs_per_check]

        for dbName in databases:
            url = urljoin(server, quote(dbName, safe=''))
            try:
                db_stats = self.agent_check.get(url, tags)
            except requests.exceptions.HTTPError as e:
                couchdb['databases'][dbName] = None
                if (e.response.status_code == 403) or (e.response.status_code == 401):
                    self.db_exclude[server].append(dbName)

                    self.warning(
                        'Database %s is not readable by the configured user. '
                        'It will be added to the exclusion list. Please restart the agent to clear.',
                        dbName,
                    )
                    del couchdb['databases'][dbName]
                    continue
            if db_stats is not None:
                couchdb['databases'][dbName] = db_stats
        return couchdb


class CouchDB2:
    """v2 of the CouchDB check. Supports all versions of Couch > 2.X, including Couch v3"""

    MAX_NODES_PER_CHECK = 20

    def __init__(self, agent_check):
        self.agent_check = agent_check
        self.gauge = agent_check.gauge

    def _build_metrics(self, data, tags, prefix='couchdb'):
        for key, value in iteritems(data):
            if "type" in value:
                if value["type"] == "histogram":
                    for metric, value in iteritems(value["value"]):
                        if metric == "histogram":
                            continue
                        elif metric == "percentile":
                            for pair in value:
                                self.gauge("{0}.{1}.percentile.{2}".format(prefix, key, pair[0]), pair[1], tags=tags)
                        else:
                            self.gauge("{0}.{1}.{2}".format(prefix, key, metric), value, tags=tags)
                else:
                    self.gauge("{0}.{1}".format(prefix, key), value["value"], tags=tags)
            elif isinstance(value, dict):
                self._build_metrics(value, tags, "{0}.{1}".format(prefix, key))

    def _build_db_metrics(self, data, tags):
        for key, value in iteritems(data['sizes']):
            self.gauge("couchdb.by_db.{0}_size".format(key), value, tags)

        for key in ['doc_del_count', 'doc_count']:
            self.gauge("couchdb.by_db.{0}".format(key), data[key], tags)

    def _build_dd_metrics(self, info, tags):
        data = info['view_index']
        ddtags = list(tags)
        ddtags.append("design_document:{0}".format(info['name']))
        ddtags.append("language:{0}".format(data['language']))

        for key, value in iteritems(data['sizes']):
            self.gauge("couchdb.by_ddoc.{0}_size".format(key), value, ddtags)

        for key, value in iteritems(data['updates_pending']):
            self.gauge("couchdb.by_ddoc.{0}_updates_pending".format(key), value, ddtags)

        self.gauge("couchdb.by_ddoc.waiting_clients", data['waiting_clients'], ddtags)

    def _build_system_metrics(self, data, tags, prefix='couchdb.erlang'):
        for key, value in iteritems(data):
            if key == "message_queues":
                for queue, val in iteritems(value):
                    queue_tags = list(tags)
                    queue_tags.append("queue:{0}".format(queue))
                    if isinstance(val, dict):
                        if 'count' in val:
                            self.gauge("{0}.{1}.size".format(prefix, key), val['count'], queue_tags)
                        else:
                            self.agent_check.log.debug(
                                "Queue %s does not have a key 'count'. It will be ignored.", queue
                            )
                    else:
                        self.gauge("{0}.{1}.size".format(prefix, key), val, queue_tags)
            elif key == "distribution":
                for node, n_metrics in iteritems(value):
                    dist_tags = list(tags)
                    dist_tags.append("node:{0}".format(node))
                    self._build_system_metrics(n_metrics, dist_tags, "{0}.{1}".format(prefix, key))
            elif isinstance(value, dict):
                self._build_system_metrics(value, tags, "{0}.{1}".format(prefix, key))
            else:
                self.gauge("{0}.{1}".format(prefix, key), value, tags)

    def _build_active_tasks_metrics(self, data, tags, prefix='couchdb.active_tasks'):
        counts = {'replication': 0, 'database_compaction': 0, 'indexer': 0, 'view_compaction': 0}
        for task in data:
            counts[task['type']] += 1
            rtags = list(tags)
            if task['type'] == 'replication':
                for tag in ['doc_id', 'source', 'target', 'user']:
                    rtags.append("{0}:{1}".format(tag, task[tag]))
                rtags.append("type:{0}".format('continuous' if task['continuous'] else 'one-time'))
                metrics = [
                    'doc_write_failures',
                    'docs_read',
                    'docs_written',
                    'missing_revisions_found',
                    'revisions_checked',
                    'changes_pending',
                ]
                for metric in metrics:
                    if task[metric] is None:
                        task[metric] = 0
                    self.gauge("{0}.replication.{1}".format(prefix, metric), task[metric], rtags)
            elif task['type'] == 'database_compaction':
                rtags.append("database:{0}".format(task['database'].split('/')[-1].split('.')[0]))
                for metric in ['changes_done', 'progress', 'total_changes']:
                    self.gauge("{0}.db_compaction.{1}".format(prefix, metric), task[metric], rtags)
            elif task['type'] == 'indexer':
                rtags.append("database:{0}".format(task['database'].split('/')[-1].split('.')[0]))
                rtags.append("design_document:{0}".format(task['design_document'].split('/')[-1]))
                for metric in ['changes_done', 'progress', 'total_changes']:
                    self.gauge("{0}.indexer.{1}".format(prefix, metric), task[metric], rtags)
            elif task['type'] == 'view_compaction':
                rtags.append("database:{0}".format(task['database'].split('/')[-1].split('.')[0]))
                rtags.append("design_document:{0}".format(task['design_document'].split('/')[-1]))
                if task.get('phase') is not None:
                    rtags.append("phase:{0}".format(task['phase']))
                for metric in ['changes_done', 'progress', 'total_changes']:
                    if task.get(metric) is not None:
                        self.gauge("{0}.view_compaction.{1}".format(prefix, metric), task[metric], rtags)

        for metric, count in iteritems(counts):
            if metric == "database_compaction":
                metric = "db_compaction"
            self.gauge("{0}.{1}.count".format(prefix, metric), count, tags)

    def _get_instance_names(self, server, instance):
        name = instance.get('name')
        if name is None:
            url = urljoin(server, "/_membership")
            names = self.agent_check.get(url, [])['cluster_nodes']
            return names[: instance.get('max_nodes_per_check', self.MAX_NODES_PER_CHECK)]
        else:
            return [name]

    def _get_dbs_to_scan(self, server, name, tags):
        dbs = self.agent_check.get(urljoin(server, "_all_dbs"), tags)
        try:
            nodes = self.agent_check.get(urljoin(server, "_membership"), tags)['cluster_nodes']
        except KeyError:
            return []

        try:
            idx = nodes.index(name)
        except ValueError:
            self.agent_check.log.error("Could not find node %r in %r", name, nodes)
            raise

        size = int(math.ceil(len(dbs) / float(len(nodes))))
        return dbs[(idx * size) : ((idx + 1) * size)]

    def check(self, instance):
        server = self.agent_check.get_server(instance)

        config_tags = self.agent_check.get_config_tags(instance)
        max_dbs_per_check = instance.get('max_dbs_per_check', self.agent_check.MAX_DB)
        for name in self._get_instance_names(server, instance):
            tags = config_tags + ["instance:{0}".format(name)]
            self._build_metrics(self._get_node_stats(server, name, tags), tags)
            self._build_system_metrics(self._get_system_stats(server, name, tags), tags)
            self._build_active_tasks_metrics(self._get_active_tasks(server, name, tags), tags)

            db_include = instance.get('db_include', instance.get('db_whitelist'))
            db_exclude = instance.get('db_exclude', instance.get('db_blacklist', []))
            scanned_dbs = 0
            for db in self._get_dbs_to_scan(server, name, tags):
                if (db_include is None or db in db_include) and (db not in db_exclude):
                    db_tags = config_tags + ["db:{0}".format(db)]
                    db_url = urljoin(server, db)
                    self._build_db_metrics(self.agent_check.get(db_url, db_tags), db_tags)
                    for dd in self.agent_check.get(
                        "{0}/_all_docs?startkey=\"_design/\"&endkey=\"_design0\"".format(db_url), db_tags
                    )['rows']:
                        self._build_dd_metrics(
                            self.agent_check.get("{0}/{1}/_info".format(db_url, dd['id']), db_tags), db_tags
                        )
                    scanned_dbs += 1
                    if scanned_dbs >= max_dbs_per_check:
                        break

    def _get_node_stats(self, server, name, tags):
        url = urljoin(server, "/_node/{0}/_stats".format(name))

        # Fetch initial stats and capture a service check based on response.
        stats = self.agent_check.get(url, tags, True)

        # No overall stats? bail out now
        if stats is None:
            raise Exception("No stats could be retrieved from %s" % url)

        return stats

    def _get_system_stats(self, server, name, tags):
        url = urljoin(server, "/_node/{0}/_system".format(name))

        # Fetch _system (Erlang) stats.
        return self.agent_check.get(url, tags)

    def _get_active_tasks(self, server, name, tags):
        url = urljoin(server, "/_active_tasks")

        tasks = self.agent_check.get(url, tags)

        #  print tasks

        return [task for task in tasks if task['node'] == name]
