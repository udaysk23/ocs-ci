# -*- coding: utf8 -*-
"""
This module contains common code and a base class for any cloud platform
deployment.
"""

import logging
import os

from ocs_ci.deployment.deployment import Deployment
from ocs_ci.deployment.ocp import OCPDeployment as BaseOCPDeployment
from ocs_ci.framework import config
from ocs_ci.ocs import constants, exceptions
from ocs_ci.utility.bootstrap import gather_bootstrap
from ocs_ci.utility.utils import get_cluster_name
from ocs_ci.utility.utils import get_infra_id
from ocs_ci.utility.utils import run_cmd


logger = logging.getLogger(__name__)


class CloudDeploymentBase(Deployment):
    """
    Base class for deployment on a cloud platform (such as AWS, Azure, ...).
    """

    def __init__(self):
        """
        Any cloud platform deployment requires region and cluster name.
        """
        super(CloudDeploymentBase, self).__init__()
        self.region = config.ENV_DATA['region']
        if config.ENV_DATA.get('cluster_name'):
            self.cluster_name = config.ENV_DATA['cluster_name']
        else:
            self.cluster_name = get_cluster_name(self.cluster_path)

    def add_volume(self, size=100):
        """
        Add a new cloud volume to all the workers.

        Args:
            size (int): Size of volume in GB (default: 100)
        """
        cluster_id = get_infra_id(self.cluster_path)
        # TODO: check if different cloud platforms requires unique patterns
        worker_pattern = f'{cluster_id}-worker*'
        logger.info(
            'Adding cloud volumes to all workers using worker pattern: %s',
            worker_pattern
        )
        self._create_cloud_volumes(worker_pattern, size)

    def _create_cloud_volumes(self, worker_pattern, size):
        """
        Add new cloud volumes to the workers. Each cloud platform has a
        different storage type which fits this use case: On AWS this should add
        EBS volumes, on Aure it should use Azure disks ...

        This private method is called from ``CloudDeploymentBase.add_volume()``
        only.

        Args:
            worker_pattern (str):  Worker name pattern e.g.:
                cluster-55jx2-worker*
            size (int): Size in GB
        """
        raise NotImplementedError("Must be Implemented in a subclass.")


class CloudIPIOCPDeployment(BaseOCPDeployment):
    """
    Common implementation of IPI OCP deployments for cloud platforms.
    """

    def __init__(self):
        super(CloudIPIOCPDeployment, self).__init__()

    def deploy_prereq(self):
        """
        Overriding deploy_prereq from parent. Perform all necessary
        prerequisites for cloud IPI here.
        """
        super(CloudIPIOCPDeployment, self).deploy_prereq()
        if config.DEPLOYMENT['preserve_bootstrap_node']:
            logger.info("Setting ENV VAR to preserve bootstrap node")
            os.environ['OPENSHIFT_INSTALL_PRESERVE_BOOTSTRAP'] = 'True'
            assert os.getenv('OPENSHIFT_INSTALL_PRESERVE_BOOTSTRAP') == 'True'

    def deploy(self, log_cli_level='DEBUG'):
        """
        Deployment specific to OCP cluster on a cloud platform.

        Args:
            log_cli_level (str): openshift installer's log level
                (default: "DEBUG")
        """
        logger.info("Deploying OCP cluster")
        logger.info(
            f"Openshift-installer will be using loglevel:{log_cli_level}"
        )
        try:
            run_cmd(
                f"{self.installer} create cluster "
                f"--dir {self.cluster_path} "
                f"--log-level {log_cli_level}",
                timeout=3600
            )
        except exceptions.CommandFailed as e:
            if constants.GATHER_BOOTSTRAP_PATTERN in str(e):
                try:
                    gather_bootstrap()
                except Exception as ex:
                    logger.error(ex)
            raise e
        self.test_cluster()
