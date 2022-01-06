# import pytest
import time
import logging
import os
import re

import requests
import json

from elasticsearch import Elasticsearch, exceptions as esexp

from ocs_ci.framework import config
from ocs_ci.framework.testlib import BaseTest
from ocs_ci.helpers.performance_lib import run_oc_command

from ocs_ci.ocs import benchmark_operator, constants, defaults, exceptions, node
from ocs_ci.ocs.elasticsearch import elasticsearch_load
from ocs_ci.ocs.exceptions import MissingRequiredConfigKeyError
from ocs_ci.ocs.ocp import OCP
from ocs_ci.ocs.resources import pod
from ocs_ci.ocs.resources.ocs import OCS
from ocs_ci.ocs.utils import get_pod_name_by_pattern
from ocs_ci.ocs.version import get_environment_info
from ocs_ci.utility.perf_dash.dashboard_api import PerfDash
from ocs_ci.utility.utils import TimeoutSampler, get_running_cluster_id

log = logging.getLogger(__name__)


class PASTest(BaseTest):
    """
    Base class for QPAS team - Performance and Scale tests

    This class contain functions which used by performance and scale test,
    and also can be used by E2E test which used the benchmark-operator (ripsaw)
    """

    def setup(self):
        """
        Setting up the environment for each performance and scale test

        Args:
            name (str): The test name that will use in the performance dashboard
        """
        log.info("Setting up test environment")
        self.crd_data = None  # place holder for Benchmark CDR data
        self.es = None  # place holder for the incluster deployment elasticsearch
        self.es_backup = None  # place holder for the elasticsearch backup
        self.main_es = None  # place holder for the main elasticsearch object
        self.benchmark_obj = None  # place holder for the benchmark object
        self.client_pod = None  # Place holder for the client pod object
        self.dev_mode = config.RUN["cli_params"].get("dev_mode")
        self.pod_obj = OCP(kind="pod", namespace=benchmark_operator.BMO_NAME)

        # Place holders for test results file (all sub-tests together)
        self.results_path = ""
        self.results_file = ""

        # Collecting all Environment configuration Software & Hardware
        # for the performance report.
        self.environment = get_environment_info()
        self.environment["clusterID"] = get_running_cluster_id()

        self.get_osd_info()

        self.get_node_info(node_type="master")
        self.get_node_info(node_type="worker")

    def teardown(self):
        if hasattr(self, "operator"):
            self.operator.cleanup()

    def get_osd_info(self):
        """
        Getting the OSD's information and update the main environment
        dictionary.

        """
        ct_pod = pod.get_ceph_tools_pod()
        osd_info = ct_pod.exec_ceph_cmd(ceph_cmd="ceph osd df")
        self.environment["osd_size"] = osd_info.get("nodes")[0].get("crush_weight")
        self.environment["osd_num"] = len(osd_info.get("nodes"))
        self.environment["total_capacity"] = osd_info.get("summary").get(
            "total_kb_avail"
        )
        self.environment["ocs_nodes_num"] = len(node.get_ocs_nodes())

    def get_node_info(self, node_type="master"):
        """
        Getting node type hardware information and update the main environment
        dictionary.

        Args:
            node_type (str): the node type to collect data about,
              can be : master / worker - the default is master

        """
        if node_type == "master":
            nodes = node.get_master_nodes()
        elif node_type == "worker":
            nodes = node.get_worker_nodes()
        else:
            log.warning(f"Node type ({node_type}) is invalid")
            return

        oc_cmd = OCP(namespace=defaults.ROOK_CLUSTER_NAMESPACE)
        self.environment[f"{node_type}_nodes_num"] = len(nodes)
        self.environment[f"{node_type}_nodes_cpu_num"] = oc_cmd.exec_oc_debug_cmd(
            node=nodes[0],
            cmd_list=["lscpu | grep '^CPU(s):' | awk '{print $NF}'"],
        ).rstrip()
        self.environment[f"{node_type}_nodes_memory"] = oc_cmd.exec_oc_debug_cmd(
            node=nodes[0], cmd_list=["free | grep Mem | awk '{print $2}'"]
        ).rstrip()

    def deploy_benchmark_operator(self):
        """
        Deploy the benchmark operator

        """
        self.operator = benchmark_operator.BenchmarkOperator()
        self.operator.deploy()

    def es_info_backup(self, elasticsearch):
        """
        Saving the Original elastic-search IP and PORT - if defined in yaml

        Args:
            elasticsearch (obj): elasticsearch object

        """

        self.crd_data["spec"]["elasticsearch"] = {}

        # for development mode use the Dev ES server
        if self.dev_mode and config.PERF.get("dev_lab_es"):
            log.info("Using the development ES server")
            self.crd_data["spec"]["elasticsearch"] = {
                "server": config.PERF.get("dev_es_server"),
                "port": config.PERF.get("dev_es_port"),
                "url": f"http://{config.PERF.get('dev_es_server')}:{config.PERF.get('dev_es_port')}",
                "parallel": True,
            }

        # for production mode use the Lab ES server
        if not self.dev_mode and config.PERF.get("production_es"):
            self.crd_data["spec"]["elasticsearch"] = {
                "server": config.PERF.get("production_es_server"),
                "port": config.PERF.get("production_es_port"),
                "url": f"http://{config.PERF.get('production_es_server')}:{config.PERF.get('production_es_port')}",
                "parallel": True,
            }

        # backup the Main ES info (if exists)
        if not self.crd_data["spec"]["elasticsearch"] == {}:
            self.backup_es = self.crd_data["spec"]["elasticsearch"]
            log.info(
                f"Creating object for the Main ES server on {self.backup_es['url']}"
            )
            self.main_es = Elasticsearch([self.backup_es["url"]], verify_certs=True)
        else:
            log.warning("Elastic Search information does not exists for this test")

        # Use the internal define elastic-search server in the test - if exist
        if elasticsearch:

            if not isinstance(elasticsearch, dict):
                # elasticsearch is an internally deployed server (obj)
                ip = elasticsearch.get_ip()
                port = elasticsearch.get_port()
            else:
                # elasticsearch is an existing server (dict)
                ip = elasticsearch.get("server")
                port = elasticsearch.get("port")

            self.crd_data["spec"]["elasticsearch"] = {
                "server": ip,
                "port": port,
                "url": f"http://{ip}:{port}",
                "parallel": True,
            }
            log.info(f"Going to use the ES : {self.crd_data['spec']['elasticsearch']}")
        elif config.PERF.get("internal_es_server"):
            # use an in-cluster elastic-search (not deployed by the test)
            self.crd_data["spec"]["elasticsearch"] = {
                "server": config.PERF.get("internal_es_server"),
                "port": config.PERF.get("internal_es_port"),
                "url": f"http://{config.PERF.get('internal_es_server')}:{config.PERF.get('internal_es_port')}",
                "parallel": True,
            }

    def set_storageclass(self, interface):
        """
        Setting the benchmark CRD storageclass

        Args:
            interface (str): The interface which will used in the test

        """
        if interface == constants.CEPHBLOCKPOOL:
            storageclass = constants.DEFAULT_STORAGECLASS_RBD
        else:
            storageclass = constants.DEFAULT_STORAGECLASS_CEPHFS
        log.info(f"Using [{storageclass}] Storageclass")
        self.crd_data["spec"]["workload"]["args"]["storageclass"] = storageclass

    def get_env_info(self):
        """
        Getting the environment information and update the workload RC if
        necessary.

        """
        if not self.environment["user"] == "":
            self.crd_data["spec"]["test_user"] = self.environment["user"]
        else:
            # since full results object need this parameter, initialize it from CR file
            self.environment["user"] = self.crd_data["spec"]["test_user"]
        self.crd_data["spec"]["clustername"] = self.environment["clustername"]

        log.debug(f"Environment information is : {self.environment}")

    def deploy_and_wait_for_wl_to_start(self, timeout=300, sleep=20):
        """
        Deploy the workload and wait until it start working

        Args:
            timeout (int): time in second to wait until the benchmark start
            sleep (int): Sleep interval seconds

        """
        log.debug(f"The {self.benchmark_name} CR file is {self.crd_data}")
        self.benchmark_obj = OCS(**self.crd_data)
        self.benchmark_obj.create()

        # This time is only for reporting - when the benchmark started.
        self.start_time = self.get_time()

        # Wait for benchmark client pod to be created
        log.info(f"Waiting for {self.client_pod_name} to Start")
        for bm_pod in TimeoutSampler(
            timeout,
            sleep,
            get_pod_name_by_pattern,
            self.client_pod_name,
            benchmark_operator.BMO_NAME,
        ):
            try:
                if bm_pod[0] is not None:
                    self.client_pod = bm_pod[0]
                    break
            except IndexError:
                log.info("Bench pod is not ready yet")
        # Sleeping for 15 sec for the client pod to be fully accessible
        time.sleep(15)
        log.info(f"The benchmark pod {self.client_pod_name} is Running")

    def wait_for_wl_to_finish(self, timeout=18000, sleep=300):
        """
        Waiting until the workload is finished and get the test log

        Args:
            timeout (int): time in second to wait until the benchmark start
            sleep (int): Sleep interval seconds

        Raise:
            exception for too much restarts of the test.
            ResourceWrongStatusException : test Failed / Error
            TimeoutExpiredError : test did not completed on time.

        """
        log.info(f"Waiting for {self.client_pod_name} to complete")

        Finished = 0
        restarts = 0
        total_time = timeout
        while not Finished and total_time > 0:
            results = run_oc_command(
                "get pod --no-headers -o custom-columns=:metadata.name,:status.phase",
                namespace=benchmark_operator.BMO_NAME,
            )
            (fname, status) = ["", ""]
            for name in results:
                # looking for the pod which run the benchmark (not the IO)
                # this pod contain the `client` in his name, and there is only one
                # pod like this, other pods have the `server` in the name.
                (fname, status) = name.split()
                if re.search("client", fname):
                    break
                else:
                    (fname, status) = ["", ""]

            if fname == "":  # there is no `client` pod !
                err_msg = f"{self.client_pod} Failed to run !!!"
                log.error(err_msg)
                raise Exception(err_msg)

            if not fname == self.client_pod:
                # The client pod name is different from previous check, it was restarted
                log.info(
                    f"The pod {self.client_pod} was restart. the new client pod is {fname}"
                )
                self.client_pod = fname
                restarts += 1
                # in case of restarting the benchmark, reset the timeout as well
                total_time = timeout

            if restarts > 3:  # we are tolerating only 3 restarts
                err_msg = f"Too much restarts of the benchmark ({restarts})"
                log.error(err_msg)
                raise Exception(err_msg)

            if status == "Succeeded":
                # Getting the end time of the benchmark - for reporting.
                self.end_time = self.get_time()
                self.test_logs = self.pod_obj.exec_oc_cmd(
                    f"logs {self.client_pod}", out_yaml_format=False
                )
                log.info(f"{self.client_pod} completed successfully")
                Finished = 1
            elif (
                status != constants.STATUS_RUNNING
                and status != constants.STATUS_PENDING
            ):
                # if the benchmark pod is not in Running state (and not Completed/Pending),
                # no need to wait for timeout.
                # Note: the pod can be in pending state in case of restart.
                err_msg = f"{self.client_pod} Failed to run - ({status})"
                log.error(err_msg)
                raise exceptions.ResourceWrongStatusException(
                    self.client_pod,
                    describe_out=err_msg,
                    column="Status",
                    expected="Succeeded",
                    got=status,
                )
            else:
                log.info(
                    f"{self.client_pod} is in {status} State, and wait to Succeeded State."
                    f" wait another {sleep} sec. for benchmark to complete"
                )
                time.sleep(sleep)
                total_time -= sleep

        if not Finished:
            err_msg = (
                f"{self.client_pod} did not completed on time, "
                f"maybe timeout ({timeout}) need to be increase"
            )
            log.error(err_msg)
            raise exceptions.TimeoutExpiredError(
                self.client_pod, custom_message=err_msg
            )

        # Saving the benchmark internal log into a file at the logs directory
        log_file_name = f"{self.full_log_path}/test-pod.log"
        try:
            with open(log_file_name, "w") as f:
                f.write(self.test_logs)
            log.info(f"The Test log can be found at : {log_file_name}")
        except Exception:
            log.warning(f"Cannot write the log to the file {log_file_name}")
        log.info(f"The {self.benchmark_name} benchmark complete")

    def copy_es_data(self, elasticsearch):
        """
        Copy data from Internal ES (if exists) to the main ES

        Args:
            elasticsearch (obj): elasticsearch object (if exits)

        """
        log.info(f"In copy_es_data Function - {elasticsearch}")
        if elasticsearch:
            log.info("Copy all data from Internal ES to Main ES")
            log.info("Dumping data from the Internal ES to tar ball file")
            elasticsearch.dumping_all_data(self.full_log_path)
            es_connection = self.backup_es
            es_connection["host"] = es_connection.pop("server")
            es_connection.pop("url")
            if elasticsearch_load(self.main_es, self.full_log_path):
                # Adding this sleep between the copy and the analyzing of the results
                # since sometimes the results of the read (just after write) are empty
                time.sleep(10)
                log.info(
                    f"All raw data for tests results can be found at : {self.full_log_path}"
                )
                return True
            else:
                log.warning("Cannot upload data into the Main ES server")
                return False

    def read_from_es(self, es, index, uuid):
        """
        Reading all results from elasticsearch server

        Args:
            es (dict): dictionary with elasticsearch info  {server, port}
            index (str): the index name to read from the elasticsearch server
            uuid (str): the test UUID to find in the elasticsearch server

        Returns:
            list : list of all results

        """

        con = Elasticsearch([{"host": es["server"], "port": es["port"]}])
        query = {"size": 1000, "query": {"match": {"uuid": uuid}}}

        try:
            results = con.search(index=index, body=query)
            full_data = []
            for res in results["hits"]["hits"]:
                full_data.append(res["_source"])
            return full_data

        except Exception as e:
            log.warning(f"{index} Not found in the Internal ES. ({e})")
            return []

    def es_connect(self):
        """
        Create elasticsearch connection to the server

        Return:
            bool : True if there is a connection to the ES, False if not.

        """

        OK = True  # the return value
        try:
            log.info(f"try to connect the ES : {self.es['server']}:{self.es['port']}")
            self.es_con = Elasticsearch(
                [{"host": self.es["server"], "port": self.es["port"]}]
            )
        except Exception:
            log.error(f"Cannot connect to ES server {self.es}")
            OK = False

        # Testing the connection to the elastic-search
        if not self.es_con.ping():
            log.error(f"Cannot connect to ES server {self.es}")
            OK = False

        return OK

    def get_kibana_indexid(self, server, name):
        """
        Get the kibana Index ID by its name.

        Args:
            server (str): the IP (or name) of the Kibana server
            name (str): the name of the index

        Returns:
            str : the index ID of the given name
                  return None if the index does not exist.

        """

        port = 5601
        http_link = f"http://{server}:{port}/api/saved_objects"
        search_string = f"_find?type=index-pattern&search_fields=title&search='{name}'"
        log.info(f"Connecting to Kibana {server} on port {port}")
        try:
            res = requests.get(f"{http_link}/{search_string}")
            res = json.loads(res.content.decode())
            for ind in res.get("saved_objects"):
                if ind.get("attributes").get("title") in [name, f"{name}*"]:
                    log.info(f"The Kibana indexID for {name} is {ind.get('id')}")
                    return ind.get("id")
        except esexp.ConnectionError:
            log.warning("Cannot connect to Kibana server {}:{}".format(server, port))
        log.warning(f"Can not find the Kibana index : {name}")
        return None

    def write_result_to_file(self, res_link):
        """
        Write the results link into file, to combine all sub-tests results
        together in one file, so it can be easily pushed into the performance dashboard

        Args:
            res_link (str): http link to the test results in the ES server

        """
        if not os.path.exists(self.results_path):
            os.makedirs(self.results_path)
        self.results_file = os.path.join(self.results_path, "all_results.txt")

        log.info(f"Try to push results into : {self.results_file}")
        try:
            with open(self.results_file, "a+") as f:
                f.write(f"{res_link}\n")
            f.close()
        except FileNotFoundError:
            log.info("The file does not exist, so create new one.")
            with open(self.results_file, "w+") as f:
                f.write(f"{res_link}\n")
            f.close()
        except OSError as err:
            log.error(f"OS error: {err}")

    @staticmethod
    def get_time():
        """
        Getting the current GMT time in a specific format for the ES report

        Returns:
            str : current date and time in formatted way

        """
        return time.strftime("%Y-%m-%dT%H:%M:%SGMT", time.gmtime())

    def check_tests_results(self):
        """
        Check that all sub-tests (test multiplication by parameters) finished and
        pushed the data to the ElastiSearch server.
        It also generate the es link to push into the performance dashboard.
        """

        es_links = []
        try:
            with open(self.results_file, "r") as f:
                data = f.read().split("\n")
            data.pop()  # remove the last empty element
            if len(data) != self.number_of_tests:
                log.error("Not all tests finished")
                raise exceptions.BenchmarkTestFailed()
            else:
                log.info("All test finished OK, and the results can be found at :")
                for res in data:
                    log.info(res)
                    es_links.append(res)
        except OSError as err:
            log.error(f"OS error: {err}")
            raise err

        self.es_link = ",".join(es_links)

    def push_to_dashboard(self, test_name):
        """
        Pushing the test results into the performance dashboard, if exist

        Args:
            test_name (str): the test name as defined in the performance dashboard

        Returns:
            None in case of pushing the results to the dashboard failed

        """

        try:
            db = PerfDash()
        except MissingRequiredConfigKeyError as ex:
            log.error(
                f"Results cannot be pushed to the performance dashboard, no connection [{ex}]"
            )
            return None

        log.info(f"Full version is : {self.environment.get('ocs_build')}")
        version = self.environment.get("ocs_build").split("-")[0]
        try:
            build = self.environment.get("ocs_build").split("-")[1]
            build = build.split(".")[0]
        except Exception:
            build = "GA"

        # Getting the topology from the cluster
        az = node.get_odf_zone_count()
        if az == 0:
            az = 1
        topology = f"{az}-AZ"

        # Check if it is Arbiter cluster
        my_obj = OCP(
            kind="StorageCluster", namespace=constants.OPENSHIFT_STORAGE_NAMESPACE
        )
        arbiter = (
            my_obj.data.get("items")[0].get("spec").get("arbiter").get("enable", False)
        )

        if arbiter:
            topology = "Strech-Arbiter"

        # Check if run on LSO
        try:
            ns = OCP(kind="namespace", resource_name=defaults.LOCAL_STORAGE_NAMESPACE)
            ns.get()
            platform = f"{self.environment.get('platform')}-LSO"
        except Exception:
            platform = self.environment.get("platform")

        # Check if encrypted cluster
        encrypt = (
            my_obj.data.get("items")[0]
            .get("spec")
            .get("encryption")
            .get("enable", False)
        )
        kms = (
            my_obj.data.get("items")[0]
            .get("spec")
            .get("encryption")
            .get("kms")
            .get("enable", False)
        )
        if kms:
            platform = f"{platform}-KMS"
        elif encrypt:
            platform = f"{platform}-Enc"

        # Check if compression is enabled
        my_obj = OCP(
            kind="cephblockpool", namespace=constants.OPENSHIFT_STORAGE_NAMESPACE
        )
        for pool in my_obj.data.get("items"):
            if pool.get("spec").get("compressionMode", None) is not None:
                platform = f"{platform}-CMP"
                break

        if self.dev_mode:
            port = "8181"
        else:
            port = "8080"

        try:
            log.info(
                "Trying to push :"
                f"version={version},"
                f"build={build},"
                f"platform={platform},"
                f"topology={topology},"
                f"test={test_name},"
                f"eslink={self.es_link}, logfile=None"
            )

            db.add_results(
                version=version,
                build=build,
                platform=platform,
                topology=topology,
                test=test_name,
                eslink=self.es_link,
                logfile=None,
            )
            resultslink = (
                f"http://{db.creds['host']}:{port}/index.php?"
                f"version1={db.get_version_id(version)}"
                f"&build1={db.get_build_id(version, build)}"
                f"&platform1={db.get_platform_id(platform)}"
                f"&az_topology1={db.get_topology_id(topology)}"
                f"&test_name%5B%5D={db.get_test_id(test_name)}"
                "&submit=Choose+options"
            )
            log.info(f"Full results report can be found at : {resultslink}")
        except Exception as ex:
            log.error(f"Can not push results into the performance Dashboard! [{ex}]")

        db.cleanup()
