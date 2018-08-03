import boto3
import botocore
import hashlib
import json
import os
import shlex
import shutil
import tempfile
import textwrap
import time

from six.moves import shlex_quote, urllib

from mlflow.entities.run_status import RunStatus
from mlflow.entities.source_type import SourceType


from mlflow.projects import ExecutionException, _fetch_project, _load_project, _expand_uri
from mlflow.projects.submitted_run import SubmittedRun
from mlflow.utils import rest_utils, file_utils, process
from mlflow.utils.logging_utils import eprint
from mlflow import tracking
from mlflow.version import VERSION

from qds_sdk.commands import ShellCommand
from qds_sdk.qubole import Qubole


# Name to use for project directory when archiving it for upload to S3; the TAR will contain
# a single directory with this name
QUBOLE_TARFILE_ARCHIVE_NAME = "mlflow-project"


def _get_qubole_run_script(run_id, entry_point, parameters):
    """
    Generates MLflow CLI command to run on Qubole cluster
    """
    project_dir = QUBOLE_TARFILE_ARCHIVE_NAME
    mlflow_run_arr = list(map(shlex_quote, ["mlflow", "run", project_dir,
                                            "--entry-point", entry_point]))
    if run_id:
        mlflow_run_arr.extend(["--run-id", run_id])
    
    if parameters:
        for key, value in parameters.items():
            mlflow_run_arr.extend(["-P", "%s=%s" % (key, value)])
    
    mlflow_run_cmd = " ".join(mlflow_run_arr)

    return mlflow_run_cmd

class S3Utils(object):
    def __init__(self, conf):
        self.bucket = conf["s3_experiment_bucket"]
        self.base_path = conf["s3_experiment_base_path"]
    
    def _get_bucket(self):
        return boto3.session.Session()\
                    .resource('s3').Bucket(self.bucket)

    def _path_exists(self, path):
        """
        Returns True if the passed-in path exists in s3.
        """
        try:
        self._get_bucket().load(path)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "404":
                return True # The object does not exist.
            else:
                raise e # Something else has gone wrong.
        else:
            return False # The object exists.

    def _upload(self, src_path, path):
        """
        Uploads the file at `src_path` to the specified S3 path.
        """
        eprint("=== Uploading project to DBFS path %s ===" % path)
        self._get_bucket().upload_file(src_path, path)
    
    def upload_project(self, project_dir, experiment_id):
        """
        Tars a project directory into an archive in a temp dir and uploads it to S3, returning
        the URI of the tarball in S3 (e.g. s3:/path/to/tar).

        :param project_dir: Path to a directory containing an MLflow project to upload to DBFS (e.g.
                            a directory containing an MLproject file).
        """
        temp_tarfile_dir = tempfile.mkdtemp()
        temp_tar_filename = file_utils.build_path(temp_tarfile_dir, "project.tar.gz")
        try:
            file_utils.make_tarfile(temp_tar_filename, project_dir, QUBOLE_TARFILE_ARCHIVE_NAME)
            with open(temp_tar_filename, "rb") as tarred_project:
                tarfile_hash = hashlib.sha256(tarred_project.read()).hexdigest()
            # TODO: Get subdirectory for experiment from the tracking server
            s3_path = os.path.join(self.base_path, str(experiment_id),
                                "projects-code", "%s.tar.gz" % tarfile_hash)
            if not self._path_exists(s3_path):
                self._upload(temp_tar_filename, s3_path)
                eprint("=== Finished uploading project to %s ===" % s3_path)
            else:
                eprint("=== Project already exists in DBFS ===")
        finally:
            shutil.rmtree(temp_tarfile_dir)
        
        full_path = os.path.join('s3:/', self.bucket, self.base_path)

        return full_path


def _run_shell_command_job(s3_path, script, env_vars, cluster_spec):
    """
    Runs the specified shell command on a Databricks cluster.
    :param s3_path: S3 path of archive
    :param script: Shell command to run
    :param env_vars: Environment variables to set in the process running `command`
    :param cluster_spec: Dictionary describing the cluster, expected to contain the fields for a
    :return: ShellCommand Object.
    """
    env_vars = " && ".join(["export {}={}" for 
                             (x, y) in env_vars.iteritems()])

    script = "{} && {}".format(env_vars, script)

    args_template = """
                    --script {} \\
                    --archives {} \\
                    --cluster-label {} \\
                    --notify {} \\
                    --tags {} \\
                    --name {} \\
                    """

    args = args_template.format(script, s3_path, 
                                cluster_spec["cluster"]["label"],
                                cluster_spec["command"]["notify"],
                                ",".join(cluster_spec["command"]["tags"])
                                cluster_spec["command"]["name"])

    eprint("=== Launched MLflow run as Qubole job ===")
    
    Qubole.configure(**cluster_spec["qubole"])
    args = ShellCommand.parse(shlex.split(args))
    command = ShellCommand.run(**args)
    
    qubol_env_base_url = "/".join(cluster_spec["qubole"]["api_url"]\
                                    .rstrip("/")\
                                    .split("/")[:-1])
    command_page_url = "{}/v2/analyze?command_id={}".format(
                            qubol_env_base_url, command.id)
    eprint("=== Check the run's status at %s ===" % command_page_url)
    return command


def _fetch_and_clean_project(uri, version=None, git_username=None, git_password=None):
    """
    Fetches the project at the passed-in URI & prepares it for upload to DBFS. Returns the path of
    the temporary directory into which the project was fetched.
    """
    work_dir = _fetch_project(
        uri=uri, force_tempdir=True, version=version, git_username=git_username,
        git_password=git_password)
    # Remove the mlruns directory from the fetched project to avoid cache-busting
    mlruns_dir = os.path.join(work_dir, "mlruns")
    if os.path.exists(mlruns_dir):
        shutil.rmtree(mlruns_dir)
    return work_dir


def _before_run_validations(tracking_uri, cluster_spec):
    """Validations to perform before running a project on Qubole."""
    if cluster_spec is None:
        raise ExecutionException("Cluster spec must be provided when launching MLflow project runs "
                                 "on Qubole.")
    if tracking.is_local_uri(tracking_uri):
        raise ExecutionException(
            "When running on Qubole, the MLflow tracking URI must be set to a remote URI "
            "accessible to both the current client and code running on Qubole. Got local "
            "tracking URI %s." % tracking_uri)


def run_qubole(uri, entry_point, version, parameters, experiment_id, cluster_spec,
                   git_username, git_password):
    """
    Runs the project at the specified URI on Databricks, returning a `SubmittedRun` that can be
    used to query the run's status or wait for the resulting Databricks Job run to terminate.
    """
    with open(cluster_spec, 'r') as handle:
        try:
            cluster_spec = json.load(handle)
        except ValueError:
            eprint("Error when attempting to load and parse JSON cluster spec from file "
                    "%s. " % cluster_spec)
            raise

    tracking_uri = tracking.get_tracking_uri()

    _before_run_validations(tracking_uri, cluster_spec)

    work_dir = _fetch_and_clean_project(
        uri=uri, version=version, git_username=git_username, git_password=git_password)
    project = _load_project(work_dir)
    project.get_entry_point(entry_point)._validate_parameters(parameters)

    s3_path = S3Utils(cluster_spec["aws"]).upload_project(work_dir, experiment_id)

    remote_run = tracking._create_run(
        experiment_id=experiment_id, source_name=_expand_uri(uri),
        source_version=tracking._get_git_commit(work_dir), entry_point_name=entry_point,
        source_type=SourceType.PROJECT)

    env_vars = {
         tracking._TRACKING_URI_ENV_VAR: tracking_uri,
         tracking._EXPERIMENT_ID_ENV_VAR: experiment_id,
    }

    run_id = remote_run.run_info.run_uuid
    eprint("=== Running entry point %s of project %s on Qubole. ===" % (entry_point, uri))
    
    # Get the shell command to run
    script = _get_qubole_run_script(run_id, entry_point, parameters)
    
    # Launch run on Qubole  
    command =  _run_shell_command_job(s3_path, script, 
                                      env_vars, cluster_spec)

    return QuboleSubmittedRun(command, run_id)


class QuboleSubmittedRun(SubmittedRun):
    """
    Instance of SubmittedRun corresponding to a Qubole Job run launched to run an MLflow
    project. Note that run_id may be None, e.g. if we did not launch the run against a tracking
    server accessible to the local client.
    """
    def __init__(self, command, run_id):
        super(QuboleSubmittedRun, self).__init__()
        self.command = command
        self.run_id = run_id

    def cancel(self):
        self.command.cancel()

    def _get_status(self):
        status = self.command.status
        if not self.command.is_done(status):
            return RunStatus.RUNNING
        if self.command.is_success(status):
            return RunStatus.FINISHED
        return RunStatus.FAILED

    def get_status(self):
        return RunStatus.to_string(self._get_status())
