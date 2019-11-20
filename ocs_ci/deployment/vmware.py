"""
This module contains platform specific methods and classes for deployment
on vSphere platform
"""
import json
import logging
import os

import hcl
import yaml

from .deployment import Deployment
from ocs_ci.deployment.install_ocp_on_rhel import OCPINSTALLRHEL
from ocs_ci.deployment.ocp import OCPDeployment as BaseOCPDeployment
from ocs_ci.deployment.terraform import Terraform
from ocs_ci.framework import config
from ocs_ci.ocs import constants
from ocs_ci.ocs.node import (
    get_node_ips, wait_for_nodes_status,
)
from ocs_ci.ocs.openshift_ops import OCP
from ocs_ci.utility.templating import Templating, dump_data_to_json
from ocs_ci.utility.utils import (
    run_cmd, replace_content_in_file, wait_for_co,
    clone_repo, upload_file, read_file_as_str,
    create_directory_path, remove_keys_from_tf_variable_file,
    convert_yaml2tfvars,
)
from ocs_ci.utility.vsphere import VSPHERE as VSPHEREUtil


logger = logging.getLogger(__name__)


# As of now only UPI
__all__ = ['VSPHEREUPI']


class VSPHEREBASE(Deployment):
    def __init__(self):
        """
        This would be base for both IPI and UPI deployment
        """
        super(VSPHEREBASE, self).__init__()
        self.region = config.ENV_DATA['region']
        self.server = config.ENV_DATA['vsphere_server']
        self.user = config.ENV_DATA['vsphere_user']
        self.password = config.ENV_DATA['vsphere_password']
        self.cluster = config.ENV_DATA['vsphere_cluster']
        self.datacenter = config.ENV_DATA['vsphere_datacenter']
        self.datastore = config.ENV_DATA['vsphere_datastore']
        self.vsphere = VSPHEREUtil(self.server, self.user, self.password)
        self.upi_repo_path = os.path.join(
            constants.EXTERNAL_DIR,
            'installer'
        )
        self.upi_scale_up_repo_path = os.path.join(
            constants.EXTERNAL_DIR,
            'openshift-misc'
        )

    def attach_disk(self, size=100):
        """
        Add a new disk to all the workers nodes

        Args:
            size (int): Size of disk in GB (default: 100)

        """
        vms = self.vsphere.get_all_vms_in_pool(
            config.ENV_DATA.get("cluster_name"),
            self.datacenter,
            self.cluster
        )
        # Add disks to all worker nodes
        for vm in vms:
            if "compute" in vm.name:
                self.vsphere.add_disks(
                    config.ENV_DATA.get("extra_disks", 1),
                    vm,
                    size,
                    constants.VM_DISK_TYPE
                )

    def add_nodes(self):
        """
        Add new nodes to the cluster
        """
        # create separate directory for scale-up terraform data
        scaleup_terraform_data_dir = os.path.join(
            self.cluster_path,
            constants.TERRAFORM_DATA_DIR,
            constants.SCALEUP_TERRAFORM_DATA_DIR
        )
        create_directory_path(scaleup_terraform_data_dir)
        logger.info(
            f"scale-up terraform data directory: {scaleup_terraform_data_dir}"
        )

        # git clone repo from openshift-misc
        clone_repo(
            constants.VSPHERE_SCALEUP_REPO, self.upi_scale_up_repo_path
        )

        # modify scale-up repo
        self.modify_scaleup_repo()

        config.ENV_DATA['vsphere_resource_pool'] = config.ENV_DATA.get(
            "cluster_name"
        )

        # get the RHCOS worker list
        self.rhcos_ips = get_node_ips()
        logger.info(f"RHCOS IP's: {json.dumps(self.rhcos_ips)}")

        # generate terraform variable for scaling nodes
        self.generate_terraform_vars_for_scaleup()

        # Add nodes using terraform
        scaleup_terraform = Terraform(constants.SCALEUP_VSPHERE_DIR)
        previous_dir = os.getcwd()
        os.chdir(scaleup_terraform_data_dir)
        scaleup_terraform.initialize()
        scaleup_terraform.apply(self.scale_up_terraform_var)
        scaleup_terraform_tfstate = os.path.join(
            scaleup_terraform_data_dir,
            "terraform.tfstate"
        )
        out = scaleup_terraform.output(
            scaleup_terraform_tfstate,
            "rhel_worker"
        )
        rhel_worker_nodes = json.loads(out)['value']
        logger.info(f"RHEL worker nodes: {rhel_worker_nodes}")
        os.chdir(previous_dir)

        # Install OCP on rhel nodes
        rhel_install = OCPINSTALLRHEL(rhel_worker_nodes)
        rhel_install.upload_helpers()
        rhel_install.install_packages_in_pod(constants.RHEL_POD_PACKAGES)
        rhel_install.prepare_rhel_nodes()
        rhel_install.execute_ansible_playbook()

        # wait for nodes to be in READY state
        wait_for_nodes_status(timeout=300)

    def generate_terraform_vars_for_scaleup(self):
        """
        Generates the terraform variables file for scaling nodes
        """
        logger.info("Generating terraform variables for scaling nodes")
        _templating = Templating()
        scale_up_terraform_var_template = "scale_up_terraform.tfvars.j2"
        scale_up_terraform_var_template_path = os.path.join(
            "ocp-deployment", scale_up_terraform_var_template
        )
        scale_up_terraform_config_str = _templating.render_template(
            scale_up_terraform_var_template_path, config.ENV_DATA
        )
        scale_up_terraform_var_yaml = os.path.join(
            self.cluster_path,
            constants.TERRAFORM_DATA_DIR,
            constants.SCALEUP_TERRAFORM_DATA_DIR,
            "scale_up_terraform.tfvars.yaml"
        )
        with open(scale_up_terraform_var_yaml, "w") as f:
            f.write(scale_up_terraform_config_str)

        self.scale_up_terraform_var = convert_yaml2tfvars(
            scale_up_terraform_var_yaml
        )
        logger.info(
            f"scale-up terraform variable file: {self.scale_up_terraform_var}"
        )

        # append RHCOS ip list to terraform variable file
        with open(self.scale_up_terraform_var, "a+") as fd:
            fd.write(f"rhcos_list = {json.dumps(self.rhcos_ips)}")

    def modify_scaleup_repo(self):
        """
        Modify the scale-up repo. Considering the user experience, removing the
        access and secret keys and variable from appropriate location in the
        scale-up repo
        """
        # remove access and secret key from constants.SCALEUP_VSPHERE_MAIN
        access_key = "access_key       = \"${var.aws_access_key}\""
        secret_key = "secret_key       = \"${var.aws_secret_key}\""
        replace_content_in_file(
            constants.SCALEUP_VSPHERE_MAIN,
            f"{access_key}",
            " "
        )
        replace_content_in_file(
            constants.SCALEUP_VSPHERE_MAIN,
            f"{secret_key}",
            " "
        )

        # remove access and secret key from constants.SCALEUP_VSPHERE_ROUTE53
        route53_access_key = "access_key = \"${var.access_key}\""
        route53_secret_key = "secret_key = \"${var.secret_key}\""
        replace_content_in_file(
            constants.SCALEUP_VSPHERE_ROUTE53,
            f"{route53_access_key}",
            " "
        )
        replace_content_in_file(
            constants.SCALEUP_VSPHERE_ROUTE53,
            f"{route53_secret_key}",
            " "
        )

        replace_content_in_file(
            constants.SCALEUP_VSPHERE_ROUTE53,
            "us-east-1",
            f"{config.ENV_DATA.get('region')}"
        )

        # remove access and secret variables from scale-up repo
        remove_keys_from_tf_variable_file(
            constants.SCALEUP_VSPHERE_VARIABLES,
            ['aws_access_key', 'aws_secret_key'])
        remove_keys_from_tf_variable_file(
            constants.SCALEUP_VSPHERE_ROUTE53_VARIABLES,
            ['access_key', 'secret_key']
        )


class VSPHEREUPI(VSPHEREBASE):
    """
    A class to handle vSphere UPI specific deployment
    """
    def __init__(self):
        super(VSPHEREUPI, self).__init__()
        self.ipam = config.ENV_DATA.get('ipam')
        self.token = config.ENV_DATA.get('ipam_token')
        self.cidr = config.ENV_DATA.get('machine_cidr')
        self.vm_network = config.ENV_DATA.get('vm_network')

    class OCPDeployment(BaseOCPDeployment):
        def __init__(self):
            super(VSPHEREUPI.OCPDeployment, self).__init__()
            self.public_key = {}
            self.upi_repo_path = os.path.join(
                constants.EXTERNAL_DIR,
                'installer'
            )
            self.previous_dir = os.getcwd()
            self.terraform_data_dir = os.path.join(self.cluster_path, constants.TERRAFORM_DATA_DIR)
            create_directory_path(self.terraform_data_dir)
            self.terraform_work_dir = constants.VSPHERE_DIR
            self.terraform = Terraform(self.terraform_work_dir)

        def deploy_prereq(self):
            """
            Pre-Requisites for vSphere UPI Deployment
            """
            super(VSPHEREUPI.OCPDeployment, self).deploy_prereq()
            # create ignitions
            self.create_ignitions()
            self.kubeconfig = os.path.join(self.cluster_path, config.RUN.get('kubeconfig_location'))

            # git clone repo from openshift installer
            clone_repo(
                constants.VSPHERE_INSTALLER_REPO, self.upi_repo_path
            )

            # upload bootstrap ignition to public access server
            bootstrap_path = os.path.join(config.ENV_DATA.get('cluster_path'), constants.BOOTSTRAP_IGN)
            remote_path = os.path.join(
                config.ENV_DATA.get('path_to_upload'),
                f"{config.RUN.get('run_id')}_{constants.BOOTSTRAP_IGN}"
            )
            upload_file(
                config.ENV_DATA.get('httpd_server'),
                bootstrap_path,
                remote_path,
                config.ENV_DATA.get('httpd_server_user'),
                config.ENV_DATA.get('httpd_server_password')
            )

            # generate bootstrap ignition url
            path_to_bootstrap_on_remote = remote_path.replace("/var/www/html/", "")
            bootstrap_ignition_url = (
                f"http://{config.ENV_DATA.get('httpd_server')}/"
                f"{path_to_bootstrap_on_remote}"
            )
            logger.info(f"bootstrap_ignition_url: {bootstrap_ignition_url}")
            config.ENV_DATA['bootstrap_ignition_url'] = bootstrap_ignition_url

            # load master and worker ignitions to variables
            master_ignition_path = os.path.join(
                config.ENV_DATA.get('cluster_path'),
                constants.MASTER_IGN
            )
            master_ignition = read_file_as_str(f"{master_ignition_path}")
            config.ENV_DATA['control_plane_ignition'] = master_ignition

            worker_ignition_path = os.path.join(
                config.ENV_DATA.get('cluster_path'),
                constants.WORKER_IGN
            )
            worker_ignition = read_file_as_str(f"{worker_ignition_path}")
            config.ENV_DATA['compute_ignition'] = worker_ignition

            cluster_domain = (
                f"{config.ENV_DATA.get('cluster_name')}."
                f"{config.ENV_DATA.get('base_domain')}"
            )
            config.ENV_DATA['cluster_domain'] = cluster_domain

            # generate terraform variables from template
            logger.info("Generating terraform variables")
            _templating = Templating()
            terraform_var_template = "terraform.tfvars.j2"
            terraform_var_template_path = os.path.join(
                "ocp-deployment", terraform_var_template
            )
            terraform_config_str = _templating.render_template(
                terraform_var_template_path, config.ENV_DATA
            )

            terraform_var_yaml = os.path.join(
                self.cluster_path,
                constants.TERRAFORM_DATA_DIR,
                "terraform.tfvars.yaml"
            )
            with open(terraform_var_yaml, "w") as f:
                f.write(terraform_config_str)
            self.terraform_var = convert_yaml2tfvars(terraform_var_yaml)

            # update gateway and DNS
            if config.ENV_DATA.get('gateway'):
                replace_content_in_file(
                    constants.INSTALLER_IGNITION,
                    '${cidrhost(var.machine_cidr,1)}',
                    f"{config.ENV_DATA.get('gateway')}"
                )

            if config.ENV_DATA.get('dns'):
                replace_content_in_file(
                    constants.INSTALLER_IGNITION,
                    constants.INSTALLER_DEFAULT_DNS,
                    f"{config.ENV_DATA.get('dns')}"
                )

            # update the zone in route
            if config.ENV_DATA.get('region'):
                def_zone = 'provider "aws" { region = "%s" } \n' % config.ENV_DATA.get('region')
                replace_content_in_file(constants.INSTALLER_ROUTE53, "xyz", def_zone)

            # increase memory
            if config.ENV_DATA.get('memory'):
                replace_content_in_file(
                    constants.INSTALLER_MACHINE_CONF,
                    '${var.memory}',
                    config.ENV_DATA.get('memory')
                )

            # increase CPUs
            worker_num_cpus = config.ENV_DATA.get('worker_num_cpus')
            master_num_cpus = config.ENV_DATA.get('master_num_cpus')
            if worker_num_cpus or master_num_cpus:
                with open(constants.VSPHERE_MAIN, 'r') as fd:
                    obj = hcl.load(fd)
                    if worker_num_cpus:
                        obj['module']['compute']['num_cpu'] = worker_num_cpus
                    if master_num_cpus:
                        obj['module']['control_plane']['num_cpu'] = master_num_cpus
                # Dump data to json file since hcl module
                # doesn't support dumping of data in HCL format
                dump_data_to_json(obj, f"{constants.VSPHERE_MAIN}.json")
                os.rename(constants.VSPHERE_MAIN, f"{constants.VSPHERE_MAIN}.backup")

        def create_config(self):
            """
            Creates the OCP deploy config for the vSphere
            """
            # Generate install-config from template
            _templating = Templating()
            ocp_install_template = (
                f"install-config-{self.deployment_platform}-"
                f"{self.deployment_type}.yaml.j2"
            )
            ocp_install_template_path = os.path.join(
                "ocp-deployment", ocp_install_template
            )
            install_config_str = _templating.render_template(
                ocp_install_template_path, config.ENV_DATA
            )

            # Parse the rendered YAML so that we can manipulate the object directly
            install_config_obj = yaml.safe_load(install_config_str)
            install_config_obj['pullSecret'] = self.get_pull_secret()
            install_config_obj['sshKey'] = self.get_ssh_key()
            install_config_str = yaml.safe_dump(install_config_obj)
            install_config = os.path.join(self.cluster_path, "install-config.yaml")
            with open(install_config, "w") as f:
                f.write(install_config_str)

        def create_ignitions(self):
            """
            Creates the ignition files
            """
            logger.info("creating ignition files for the cluster")
            run_cmd(
                f"{self.installer} create ignition-configs "
                f"--dir {self.cluster_path} "
            )

        def configure_storage_for_image_registry(self, kubeconfig):
            """
            Configures storage for the image registry
            """
            logger.info("configuring storage for image registry")
            patch = " '{\"spec\":{\"storage\":{\"emptyDir\":{}}}}' "
            run_cmd(
                f"oc --kubeconfig {kubeconfig} patch "
                f"configs.imageregistry.operator.openshift.io "
                f"cluster --type merge --patch {patch}"
            )

        def deploy(self, log_cli_level='DEBUG'):
            """
            Deployment specific to OCP cluster on this platform

            Args:
                log_cli_level (str): openshift installer's log level
                    (default: "DEBUG")

            """
            logger.info("Deploying OCP cluster for vSphere platform")
            logger.info(
                f"Openshift-installer will be using loglevel:{log_cli_level}"
            )
            os.chdir(self.terraform_data_dir)
            self.terraform.initialize()
            self.terraform.apply(self.terraform_var)
            os.chdir(self.previous_dir)
            logger.info("waiting for bootstrap to complete")
            run_cmd(
                f"{self.installer} wait-for bootstrap-complete "
                f"--dir {self.cluster_path} "
                f"--log-level {log_cli_level}",
                timeout=3600
            )
            logger.info("removing bootstrap node")
            os.chdir(self.terraform_data_dir)
            self.terraform.apply(self.terraform_var, bootstrap_complete=True)
            os.chdir(self.previous_dir)

            OCP.set_kubeconfig(self.kubeconfig)
            # wait for image registry to show-up
            co = "image-registry"
            wait_for_co(co)

            # patch image registry to null
            self.configure_storage_for_image_registry(self.kubeconfig)

            # wait for install to complete
            logger.info("waiting for install to complete")
            run_cmd(
                f"{self.installer} wait-for install-complete "
                f"--dir {self.cluster_path} "
                f"--log-level {log_cli_level}",
                timeout=1800
            )

            self.test_cluster()

    def deploy_ocp(self, log_cli_level='DEBUG'):
        """
        Deployment specific to OCP cluster on vSphere platform

        Args:
            log_cli_level (str): openshift installer's log level
                (default: "DEBUG")

        """
        super(VSPHEREUPI, self).deploy_ocp(log_cli_level)
        if config.ENV_DATA.get('scale_up'):
            logger.info("Adding extra nodes to cluster")
            self.add_nodes()

    def destroy_cluster(self, log_level="DEBUG"):
        """
        Destroy OCP cluster specific to vSphere UPI

        Args:
            log_level (str): log level openshift-installer (default: DEBUG)

        """
        previous_dir = os.getcwd()
        terraform_data_dir = os.path.join(self.cluster_path, constants.TERRAFORM_DATA_DIR)
        upi_repo_path = os.path.join(
            constants.EXTERNAL_DIR, 'installer',
        )
        tfvars = os.path.join(
            config.ENV_DATA.get('cluster_path'),
            constants.TERRAFORM_DATA_DIR,
            constants.TERRAFORM_VARS
        )
        clone_repo(
            constants.VSPHERE_INSTALLER_REPO, upi_repo_path
        )
        if (
            os.path.exists(f"{constants.VSPHERE_MAIN}.backup")
            and os.path.exists(f"{constants.VSPHERE_MAIN}.json")
        ):
            os.rename(f"{constants.VSPHERE_MAIN}.json", f"{constants.VSPHERE_MAIN}.json.backup")
        terraform = Terraform(os.path.join(upi_repo_path, "upi/vsphere/"))
        os.chdir(terraform_data_dir)
        terraform.initialize(upgrade=True)
        terraform.destroy(tfvars)
        os.chdir(previous_dir)
