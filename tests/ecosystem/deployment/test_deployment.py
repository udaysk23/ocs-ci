import logging

from ocs_ci.framework import config
from ocs_ci.framework.testlib import deployment, destroy
from ocs_ci.utility.utils import is_cluster_running

log = logging.getLogger(__name__)


# @destroy marker is added only for smooth transition in CI/Jenkins jobs,
# will be removed in one or two weeks
@destroy
@deployment
def test_deployment():
    deploy = config.RUN['cli_params'].get('deploy')
    teardown = config.RUN['cli_params'].get('teardown')
    if not teardown or deploy:
        assert is_cluster_running(config.ENV_DATA['cluster_path'])

    if teardown:
        log.info(
            "Cluster will be destroyed during teardown part of this test."
        )
