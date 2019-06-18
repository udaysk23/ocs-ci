import pytest
import json
import logging
import os
import time
import yaml
import copy

from ocsci import config
from utility.environment_check import environment_checker  # noqa: F401
from oc.openshift_ops import OCP
from ocs import constants, ocp
from ocs.exceptions import CommandFailed, CephHealthException
from ocs.utils import create_oc_resource, apply_oc_resource
from utility import templating
from utility.aws import AWS
from utility.retry import retry
from utility.utils import destroy_cluster, run_cmd, get_openshift_installer, get_openshift_client, is_cluster_running
from ocs.parallel import parallel
from resources.ocs import OCS
from tests import helpers

log = logging.getLogger(__name__)

POD = ocp.OCP(kind=constants.POD, namespace=config.ENV_DATA['cluster_namespace'])
CFS = ocp.OCP(
    kind=constants.CEPHFILESYSTEM, namespace=config.ENV_DATA['cluster_namespace']
)
CEPH_OBJ = None


@pytest.fixture(
    params=[{
        'storageclass_name': 'invalid-storageclass',
        'provisioner': "invalid_provisioner",
        'monitors': 'invalid_monitors',
        'provision_volume': "invalid_provisioner_volume",
        'ceph_pool': 'invalid_pool',
        'root_path': 'invalid_root_path',
        'provisioner_secret_name': 'invalid_provisioner_secret_name',
        'provisioner_secret_namespace': 'invalid_provisioner_secret_namespace',
        'node_stage_secret_name': 'invalid_node_stage_secret_name',
        'node_stage_secret_namespace': 'invalid_node_stage_secret_namespace',
        'mounter': 'invalid_mounter',
        'reclaim_policy': 'Delete'
    }]  # TODO: add more test case parameters
)
def invalid_cephfs_storageclass(request):
    """
    Creates StorageClass with CephFS filesystem that have invalid parameters.
    Storageclass is removed at the end of test.

    Returns:
        str: Name of created StorageClass
    """
    log.info(
        f"SETUP - creating storageclass "
        f"{request.param['storageclass_name']}"
    )
    yaml_path = os.path.join(
        constants.TEMPLATE_CSI_FS_DIR, "storageclass.yaml"
    )
    yaml_data = yaml.safe_load(open(yaml_path, 'r'))
    yaml_data.update(request.param)
    storageclass = OCS(**yaml_data)
    sc_data = storageclass.create()

    log.debug('Check that storageclass has assigned creationTimestamp')
    assert sc_data['metadata']['creationTimestamp']

    yield sc_data

    log.info(
        f"TEARDOWN - removing storageclass "
        f"{request.param['storageclass_name']}"
    )
    storageclass.delete()


def polarion_testsuite_properties(record_testsuite_property):
    """
    Configures polarion testsuite properties for junit xml
    """
    polarion_project_id = config.REPORTING['polarion']['project_id']
    record_testsuite_property('polarion-projct-id', polarion_project_id)


def cluster_teardown():
    log.info("Destroying the test cluster")
    destroy_cluster(ENV_DATA['cluster_path'])
    log.info("Destroying the test cluster complete")


@pytest.fixture(scope="session", autouse=True)
def cluster(request):
    log.info("Running OCS basic installation")
    cluster_path = config.ENV_DATA['cluster_path']
    # Add a finalizer to teardown the cluster after test execution is finished
    if config.teardown:
        request.addfinalizer(cluster_teardown)
        log.info("Will teardown cluster because --teardown was provided")
    # Test cluster access and if exist just skip the deployment.
    if is_cluster_running(cluster_path):
        log.info("The installation is skipped because the cluster is running")
        return
    elif config.teardown and not config.deploy:
        log.info("Attempting teardown of non-accessible cluster: %s", cluster_path)
        return
    elif not config.deploy and not config.teardown:
        msg = "The given cluster can not be connected to: {}. ".format(cluster_path)
        msg += "Provide a valid --cluster-path or use --deploy to deploy a new cluster"
        pytest.fail(msg)
    else:
        log.info("A testing cluster will be deployed and cluster information stored at: %s", cluster_path)

    # Generate install-config from template
    log.info("Generating install-config")
    run_cmd(f"mkdir -p {cluster_path}")
    pull_secret_path = os.path.join(
        constants.TOP_DIR,
        "data",
        "pull-secret"
    )

    # TODO: check for supported platform and raise the exception if not
    # supported. Currently we support just AWS.

    _templating = templating.Templating()
    install_config_str = _templating.render_template(
        "install-config.yaml.j2", config.ENV_DATA
    )
    # Parse the rendered YAML so that we can manipulate the object directly
    install_config_obj = yaml.safe_load(install_config_str)
    with open(pull_secret_path, "r") as f:
        # Parse, then unparse, the JSON file.
        # We do this for two reasons: to ensure it is well-formatted, and
        # also to ensure it ends up as a single line.
        install_config_obj['pullSecret'] = json.dumps(json.loads(f.read()))
    install_config_str = yaml.safe_dump(install_config_obj)
    log.info(f"Install config: \n{install_config_str}")
    install_config = os.path.join(cluster_path, "install-config.yaml")
    with open(install_config, "w") as f:
        f.write(install_config_str)

    # Download installer
    installer = get_openshift_installer(
        config.DEPLOYMENT['installer_version']
    )
    # Download client
    get_openshift_client()

    # Deploy cluster
    log.info("Deploying cluster")
    run_cmd(
        f"{installer} create cluster "
        f"--dir {cluster_path} "
        f"--log-level debug"
    )

    # Test cluster access
    if not OCP.set_kubeconfig(
        os.path.join(cluster_path, config.RUN.get('kubeconfig_location'))
    ):
        pytest.fail("Cluster is not available!")

    # TODO: Create cluster object, add to config.ENV_DATA for other tests to
    # utilize.
    # Determine worker pattern and create ebs volumes
    with open(os.path.join(cluster_path, "terraform.tfvars")) as f:
        tfvars = json.load(f)

    cluster_id = tfvars['cluster_id']
    worker_pattern = f'{cluster_id}-worker*'
    log.info(f'Worker pattern: {worker_pattern}')
    create_ebs_volumes(worker_pattern, region_name=config.ENV_DATA['region'])

    # render templates and create resources
    create_oc_resource('common.yaml', cluster_path, _templating, config.ENV_DATA)
    run_cmd(
        f'oc label namespace {config.ENV_DATA["cluster_namespace"]} '
        f'"openshift.io/cluster-monitoring=true"'
    )
    run_cmd(
        f"oc policy add-role-to-user view "
        f"system:serviceaccount:openshift-monitoring:prometheus-k8s "
        f"-n {config.ENV_DATA['cluster_namespace']}"
    )
    apply_oc_resource(
        'csi-nodeplugin-rbac_rbd.yaml',
        cluster_path,
        _templating,
        config.ENV_DATA,
        template_dir="ocs-deployment/csi/rbd/"
    )
    apply_oc_resource(
        'csi-provisioner-rbac_rbd.yaml',
        cluster_path,
        _templating,
        config.ENV_DATA,
        template_dir="ocs-deployment/csi/rbd/"
    )
    apply_oc_resource(
        'csi-nodeplugin-rbac_cephfs.yaml',
        cluster_path,
        _templating,
        config.ENV_DATA,
        template_dir="ocs-deployment/csi/cephfs/"
    )
    apply_oc_resource(
        'csi-provisioner-rbac_cephfs.yaml',
        cluster_path,
        _templating,
        config.ENV_DATA,
        template_dir="ocs-deployment/csi/cephfs/"
    )
    # Increased to 15 seconds as 10 is not enough
    # TODO: do the sampler function and check if resource exist
    wait_time = 15
    log.info(f"Waiting {wait_time} seconds...")
    time.sleep(wait_time)
    create_oc_resource(
        'operator-openshift-with-csi.yaml', cluster_path, _templating, config.ENV_DATA
    )
    log.info(f"Waiting {wait_time} seconds...")
    time.sleep(wait_time)
    run_cmd(
        f"oc wait --for condition=ready pod "
        f"-l app=rook-ceph-operator "
        f"-n {config.ENV_DATA['cluster_namespace']} "
        f"--timeout=120s"
    )
    run_cmd(
        f"oc wait --for condition=ready pod "
        f"-l app=rook-discover "
        f"-n {config.ENV_DATA['cluster_namespace']} "
        f"--timeout=120s"
    )
    create_oc_resource('cluster.yaml', cluster_path, _templating, config.ENV_DATA)

    # Check for the Running status of Ceph Pods
    run_cmd(
        f"oc wait --for condition=ready pod "
        f"-l app=rook-ceph-agent "
        f"-n {config.ENV_DATA['cluster_namespace']} "
        f"--timeout=120s"
    )
    assert POD.wait_for_resource(
        condition='Running', selector='app=rook-ceph-mon',
        resource_count=3, timeout=600
    )
    assert POD.wait_for_resource(
        condition='Running', selector='app=rook-ceph-mgr',
        timeout=600
    )
    assert POD.wait_for_resource(
        condition='Running', selector='app=rook-ceph-osd',
        resource_count=3, timeout=600
    )

    create_oc_resource('toolbox.yaml', cluster_path, _templating, config.ENV_DATA)
    log.info(f"Waiting {wait_time} seconds...")
    time.sleep(wait_time)
    create_oc_resource(
        'storage-manifest.yaml', cluster_path, _templating, config.ENV_DATA
    )
    create_oc_resource(
        "service-monitor.yaml", cluster_path, _templating, config.ENV_DATA
    )
    create_oc_resource(
        "prometheus-rules.yaml", cluster_path, _templating, config.ENV_DATA
    )
    log.info(f"Waiting {wait_time} seconds...")
    time.sleep(wait_time)

    # Create MDS pods for CephFileSystem
    fs_data = copy.deepcopy(defaults.CEPHFILESYSTEM_DICT)
    fs_data['metadata']['namespace'] = config.ENV_DATA['cluster_namespace']

    global CEPH_OBJ
    CEPH_OBJ = OCS(**fs_data)
    CEPH_OBJ.create()
    assert POD.wait_for_resource(
        condition=constants.STATUS_RUNNING, selector='app=rook-ceph-mds',
        resource_count=2, timeout=600
    )

    # Check for CephFilesystem creation in ocp
    cfs_data = CFS.get()
    cfs_name = cfs_data['items'][0]['metadata']['name']

    if helpers.validate_cephfilesystem(cfs_name):
        log.info(f"MDS deployment is successful!")
    else:
        log.error(
            f"MDS deployment Failed! Please check logs!"
        )

    # Verify health of ceph cluster
    # TODO: move destroy cluster logic to new CLI usage pattern?
    log.info("Done creating rook resources, waiting for HEALTH_OK")
    assert ceph_health_check(namespace=config.ENV_DATA['cluster_namespace'])


def create_ebs_volumes(
    worker_pattern,
    size=100,
    region_name=config.ENV_DATA['region'],
):
    """
    Create volumes on workers

    Args:
        worker_pattern (string): Worker name pattern e.g.:
            cluster-55jx2-worker*
        size (int): Size in GB (default: 100)
        region_name (str): Region name (default: config.ENV_DATA['region'])
    """
    aws = AWS(region_name)
    worker_instances = aws.get_instances_by_name_pattern(worker_pattern)
    with parallel() as p:
        for worker in worker_instances:
            log.info(
                f"Creating and attaching {size} GB volume to {worker['name']}"
            )
            p.spawn(
                aws.create_volume_and_attach,
                availability_zone=worker['avz'],
                instance_id=worker['id'],
                name=f"{worker['name']}_extra_volume",
                size=size,
            )


@retry((CephHealthException, CommandFailed), tries=20, delay=30, backoff=1)
def ceph_health_check(namespace=config.ENV_DATA['cluster_namespace']):
    """
    Exec `ceph health` cmd on tools pod to determine health of cluster.

    Args:
        namespace (str): Namespace of OCS
            (default: config.ENV_DATA['cluster_namespace'])

    Raises:
        CephHealthException: If the ceph health returned is not HEALTH_OK
        CommandFailed: If the command to retrieve the tools pod name or the
            command to get ceph health returns a non-zero exit code
    Returns:
        boolean: True if HEALTH_OK

    """
    run_cmd(
        f"oc wait --for condition=ready pod "
        f"-l app=rook-ceph-tools "
        f"-n {namespace} "
        f"--timeout=120s"
    )
    tools_pod = run_cmd(
        f"oc -n {namespace} get pod -l 'app=rook-ceph-tools' "
        f"-o jsonpath='{{.items[0].metadata.name}}'"
    )
    health = run_cmd(f"oc -n {namespace} exec {tools_pod} ceph health")
    if health.strip() == "HEALTH_OK":
        log.info("HEALTH_OK, install successful.")
        return True
    else:
        raise CephHealthException(
            f"Ceph cluster health is not OK. Health: {health}"
        )
