# Running MLFlow in Qubole Mode


When run in `"qubole"` mode, a `ShellCommand` is launched on QDS from the MLFlow project. 

## Setting up cluster

Install `mlflow` package on cluster using the node-bootstrap.

```
# Define a conda environment file for an environment with mlflow
rm -rf /usr/lib/envs
mkdir /usr/lib/envs

conda_yaml="name: mlflow
channels:
  - defaults
dependencies:
  - pyaml
  - pip:
    - https://github.com/qubole/mlflow/releases/download/v0.7.0-q/mlflow-0.7.0.dev0-py3-none-any.whl"

# Store spec file
echo "$conda_yaml" >> conda.yaml
# Create the new envrionment from package management py 3.5 base environment
/usr/lib/a-4.2.0-py-3.5.3/bin/conda env create -p /usr/lib/envs/mlflow -f conda.yaml
# Remove the spec file
rm -f conda.yaml
```

## Start tracking server

To run a long-lived, shared MLflow tracking server, launch an EC2 instance to run the MLflow Tracking server.

Create an Anaconda with Python 3 AMI EC2 instance.You can use a t2.micro (Free-tier) instance for test environment. This AMI already has conda and many other packages needed pre-installed.
Install mlflow: pip install mlflow.
Open port 5000 for MLflow server; an example of how to do this via How to open a web server port on EC2 instance. Opening up port 5000 to the Internet will allow anyone to access your server, so it is recommended to only open up the port within an AWS VPC that your Qubole clusters have access to.
Configure your AWS credentials on the instance. The optimal configuration for MLflow Remote Tracking is to use the default-artifact-root option to store your artifacts in an S3 bucket.
SSH into your EC2 instance, e.g. ssh -i ~/.ssh/<key>.pem ubuntu@<hostname>.<region>.compute.amazonaws.com.
Configure your S3 credentials via aws cli; for more information, refer to Configuring the AWS CLI.
Run the Tracking Server
Start the tracking server: 
```sh
mlflow server --default-artifact-root s3://<bucket-name> --host 0.0.0.0.
```
For more information, refer to MLflow > Running a Tracking Server.
Test connectivity of your tracking server. Go to http://<mlflow-server-dns>:5000; it should look similar to

![](https://docs.databricks.com/_static/images/mlflow/mlflow-web-ui.png)

## Run the job

### Install Qubole MLFlow

Create a virtual environment and install `mlflow`,

```sh
virtualenv mlflow_env
source mlflow_env/bin/activate
pip install https://github.com/qubole/mlflow/releases/download/v0.7.0-q/mlflow-0.7.0.dev0-py3-none-any.whl
```

### Create cluster spec file
Running the remote job requires `cluster-spec.json` to be passed as follows,

```json
{
    "aws": {
        "s3_experiment_bucket": "<bucket-name>",
        "s3_experiment_base_path": "<directory>"
    },
    "qubole": {
        "api_token": "<qubole-api-token>" ,
        "api_url": "https://api.qubole.com/api/",
        "version": "v1.2",
        "poll_interval": 5,
        "skip_ssl_cert_check": false,
        "cloud_name": "AWS"
    },
    "cluster": {
        "label": "mlflow-test"
    },
    "command": {
        "name": "mlflow-test",
        "tags": ["mlflow"],
        "notify": false
    }
}
```

### Set tracking server variable

Set environment variable `MLFLOW_TRACKING_URI`.

```sh
export MLFLOW_TRACKING_URI=http://<mlflow-server-dns>:5000
```

### Example

A toy example can be launch using the following command,

```sh
mlflow run git@github.com:mlflow/mlflow-example.git -P alpha=0.5 -m qubole --cluster-spec example/qubole_run_remote/cluster_spec.json
```