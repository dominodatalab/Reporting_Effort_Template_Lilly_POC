#!/usr/bin/env python3
"""
This program allows execution of multiple Domino jobs based on a configuration file.

The configuration file may contain a directed, acyclic graph of jobs to run.  Each
job consists of a command and optionally an environment, hardware tier, git tag, and
input and output dependencies.  Multijob.py will use this information allong with
instructions provided through command line options to execute the defined jobs in
the appropriate order.  Jobs without dependencies may run in parallel.
"""

from argparse import ArgumentParser
import backoff
import configparser
import logging
import os
import os.path
from pathlib import Path
import sys
import time
import pprint
import logging
import re
import requests
import subprocess
import threading
from requests.exceptions import HTTPError
from http import HTTPStatus

retry_codes = [
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
]

class RetryException(Exception):
    def __init__(self, exception):
        self._exception = exception

    def __getattr__(self, attr):
        return getattr(self._exception, attr)

    def __str__(self):
        return str(self._exception)

    def __repr__(self):
        return repr(self._exception)

"""
on each tick:
- iterate through list of tasks and check status of dependencies.
- return a list of tasks that are elibible to be submitted.
- submit eligible runs
"""

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

import json
from time import sleep
from datetime import datetime

DOMINO_RUN_ID = os.environ['DOMINO_RUN_ID']
DOMINO_STARTING_USERNAME = os.environ['DOMINO_STARTING_USERNAME']
DOMINO_API_HOST = os.environ['DOMINO_API_PROXY']
DOMINO_PROJECT_ID = os.environ['DOMINO_PROJECT_ID']
DOMINO_PROJECT_OWNER = os.environ['DOMINO_PROJECT_OWNER']
DOMINO_PROJECT_NAME = os.environ['DOMINO_PROJECT_NAME']
DOMINO_IS_GIT_BASED = os.environ['DOMINO_IS_GIT_BASED']

# These variables may not be set in the project, which we should interpret as a 'false' value
try:
    CXRUN = os.environ['DMV_ISCX'].lower()
    logger.info(f"DMV_ISCX is set to {CXRUN}")
except KeyError:
    CXRUN = 'false'

try:
    PRERUN_CLEANUP = os.environ['DMV_PREP'].lower()
    logger.info(f"DMV_PREP is set to {PRERUN_CLEANUP}")
except KeyError:
    PRERUN_CLEANUP = 'false'

KEEP_EMPTY_LOGS = False
FORCE_RERUN = False


class LogThread(threading.Thread):
    def __init__(self, task_id, in_pipe, log_file_path, is_error=False, add_timestamp=True):
        super().__init__()
        self.task_id = task_id
        self.in_pipe = in_pipe
        self.log_file_path = log_file_path
        self.is_error = is_error
        t = "err" if is_error else "out"
        logger_name = f'{__name__}.{task_id}.{t}'
        self.logger = logging.getLogger(logger_name)
        handler = logging.FileHandler(log_file_path, mode="w", encoding="utf8", delay=False)
        handler.setLevel(logging.INFO)
        if add_timestamp:
            formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
        else:
            formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.propagate = False

    def run(self):
        t = "error" if self.is_error else "output"
        logger.debug(f"Started {t} logging thread for {self.task_id}")
        with self.in_pipe:
            for line in iter(self.in_pipe.readline, b''):  # b'\n'-separated lines
                if self.is_error:
                    self.logger.error(line.decode().rstrip())
                else:
                    self.logger.info(line.decode().rstrip())
        logger.debug(f"Terminating {t} logging thread for {self.task_id}")
        self.logger.handlers.clear()


def extract_program_name(command_line):
    # Split the command line into parts
    parts = command_line.split()

    # Strip off initial interpreter
    while parts[0] in ['R', 'sas', 'python', 'python3', 'node']:
        parts = parts[1:]

    # The first part is the program name with its path
    program_with_path = parts[0]

    # Split the program_with_path into path and program name
    path_parts = program_with_path.split('/')

    # The last part is the program name
    program_name = path_parts[-1]

    return program_name


def start_background_job(task, command, out_log_path, err_log_path, add_timestamp):
    logger.info(f'{task}: Starting command: {command}')

    # Launch the command as a background job
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    out_thread = LogThread(task, process.stdout, out_log_path, False, add_timestamp)
    out_thread.start()
    err_thread = LogThread(task, process.stderr, err_log_path, True, add_timestamp)
    err_thread.start()

    return process, out_thread, err_thread


class DominoRun:
    """
    self.task_id        # name of task
    self.command        # command to submit to API
    self.isDirect       # isDirect flag to submit to API
    self.max_retries    # maximum retries

    self.job_id        # ID of latest run attempt
    self.retries        # number of retries so far
    self.status()       # check API for status - stop checking once Succeeded or (Error/Failed and self.retries < self.max_retries)
    self._status        # last .status()
    
    once submitted, it polls status, and retries (submits re-runs) up to max_retries

    self.process        # the Popen object for tracking locally run jobs
    """
    def __init__(self, task_id, command, inputs, outputs, max_retries=0, tier=None, environment=None, project_repo_git_ref=None, imported_repo_git_refs=None):
        self.task_id = task_id
        self.command = command
        self.inputs = inputs
        self.outputs = outputs
        self.max_retries = max_retries
        self.tier = tier
        self.environment = environment
        self.project_repo_git_ref = project_repo_git_ref
        self.imported_repo_git_refs = imported_repo_git_refs
        self.job_id = None
        self.retries = 0
        self._status = "Unsubmitted"
        self.process = None
        self.success_codes = (0, )
        self.out_thread = None
        self.err_thread = None
        self.start_time = None

    def status(self):
        global KEEP_EMPTY_LOGS
        if self.process is None and self.job_id is not None:
            # call Domino to get the job status
            if self._status not in ("Succeeded", "Unsubmitted", "Error", "Failed", "Stopped"):
                job_status = get_job_status(self.job_id)
                self.set_status(job_status)
                if self._status == 'Succeeded':
                    # If the job succeeded, touch the output files.  This is done to work around file
                    # attribute caching in NFS which causes the update to the file done in the remote
                    # job not to be seen right away by other NFS clients of the same file system.
                    for output in self.outputs:
                        Path(output).touch()
        elif self.process is not None:
            # check the status of local process
            returncode = self.process.poll()
            if returncode is not None:
                # handle local process completion
                job_status = "Succeeded" if returncode in self.success_codes else 'Failed'
                if job_status == "Succeeded":
                    logger.info(f"{self.task_id}: Command {job_status.lower()} with exit code {returncode}")
                else:
                    logger.error(f"{self.task_id}: Command {job_status.lower()} with exit code {returncode}")
                self.set_status(job_status)
                self.process = None
                self.out_thread.join()
                self.err_thread.join()
                task_duration = time.time() - self.start_time
                logger.info(f"{self.task_id}: Completed in {task_duration:.2f} seconds.")

                # remove empty log files, unless --keep option is set
                if os.path.getsize(self.out_thread.log_file_path) == 0 and not KEEP_EMPTY_LOGS:
                    logger.info(f"{self.task_id}: Removing empty log file {self.out_thread.log_file_path}")
                    os.remove(self.out_thread.log_file_path)
                else:
                    logger.info(f"{self.task_id}: Output log {self.out_thread.log_file_path}")

                if os.path.getsize(self.err_thread.log_file_path) == 0 and not KEEP_EMPTY_LOGS:
                    logger.info(f"{self.task_id}: Removing empty log file {self.err_thread.log_file_path}")
                    os.remove(self.err_thread.log_file_path)
                else:
                    logger.info(f"{self.task_id}: Error log {self.err_thread.log_file_path}")

                self.out_thread = None
                self.err_thread = None
            else:
                # local process is still running
                self.set_status('Submitted')

        return self._status

    def set_status(self, status):
        self._status = status
        if self.start_time is None and self._status not in ("Succeeded", "Unsubmitted", "Error", "Failed", "Stopped"):
            self.start_time = time.time()

    def is_complete(self):
        return self.status() == "Succeeded"

    def __str__(self):
        return f"Task '{self.task_id}' with status '{self._status}' for command '{self.command}'"


class Dag:
    """
    self.tasks              # dictionary of task_ids -> DominoRun objects
    self.dependency_graph   # dictionary of task_ids -> list of dependency task_ids
    """
    def __init__(self, tasks, dependency_graph, allow_partial_failure=False):
        self.tasks = tasks
        self.dependency_graph = dependency_graph
        self.allow_partial_failure = allow_partial_failure

    def get_dependency_statuses(self, task_id):
        dependency_statuses = []
        deps = self.dependency_graph[task_id]
        for dep in deps:
            dependency_statuses += [self.tasks[dep].status()]
        return dependency_statuses

    def are_task_dependencies_complete(self, task_id):
        dependency_statuses = self.get_dependency_statuses(task_id)
        if dependency_statuses:
            all_deps_succeeded = all(status == 'Succeeded' for status in dependency_statuses)
        else:
            all_deps_succeeded = True
        return all_deps_succeeded

    def get_ready_tasks(self):
        global FORCE_RERUN
        ready_tasks = []
        for task_id, task in self.tasks.items():
            task.status()  # update the task status - do it once, since it could be expensive
            deps_complete = self.are_task_dependencies_complete(task_id)
            rerun_required = deps_complete and (FORCE_RERUN or self.is_rerun_required(task))
            if task._status == 'Unsubmitted' and deps_complete and not rerun_required:
                # Skip task if all dependencies are satisfied
                task.set_status("Succeeded")
                logger.info(f"Dependencies satisfied. Skipping task {task.task_id}.")
            task_status_ready = (task._status in ("Error", "Failed") and task.retries < task.max_retries) or task._status == 'Unsubmitted'
            if deps_complete and task_status_ready:
                ready_tasks.append(task)
        return ready_tasks

    def is_rerun_required(self, task):
        """
        Algorithm:
        1. For each task, check if all the output files exists.
        2. If any output file does not exist, rerun is required.
        3. If all the output files exists, compare the modification times of the output files and the input files.
        4. If any input file is newer than any output file, rerun is required.
        5. If the task has no defined inputs or no defined outputs, rerun is required.
        6. If no input file is newer than an output file, skip the task.
        """
        rerun = False
        if any([not os.path.exists(s) for s in task.outputs]):
            rerun = True
        elif len(task.inputs) == 0 or len(task.outputs) == 0 or max([os.path.getmtime(s) for s in task.inputs]) > min([os.path.getmtime(s) for s in task.outputs]):
            rerun = True
        return rerun

    def count_local_submitted_jobs(self):
        count = 0
        for task_id, task in self.tasks.items():
            task.status()  # update internal status
            if task.process is not None:
                count += 1
        return count

    def get_failed_tasks(self):
        failed_tasks = []
        for task_id, task in self.tasks.items():
            if task.status() in ('Error', 'Failed') and task.retries >= task.max_retries:
                failed_tasks.append(task)
        return failed_tasks

    def pipeline_status(self):
        status = 'Running'
        if len(self.get_failed_tasks()) > 0 and self.allow_partial_failure == False:
            status = 'Failed'
        elif all(task.is_complete() for task_id, task in self.tasks.items()):
            status = 'Succeeded'
        return status

    def validate_dag(self):
        def recurse(task, original_task):
            dag_valid = True
            for dependency in self.dependency_graph[task]:
                if dependency not in self.dependency_graph.keys():
                    dag_valid = False
                    logger.error(f"ERROR: Dependency '{dependency}' is not found in the graph.")
                elif original_task in self.dependency_graph[dependency]:
                    dag_valid = False
                    logger.error(f"ERROR: Circular dependency detected. {original_task} won't start because {task} depends on {dependency}, but {dependency} depends on {original_task}.\nPlease review your config and resolve any circular references.")
                if dag_valid == False:
                    break
                else:
                    dag_valid = recurse(dependency, original_task)
        
            return dag_valid

        for task in self.dependency_graph:
            dag_valid = recurse(task, task)
        
            if not dag_valid:
                logger.error('ERROR: Exiting due to invalid dependency structure.')
                exit(1)

    def validate_run_command(self):
        pass

    def __str__(self):
        return pprint.pformat(self.dependency_graph, width=132, compact=True)

def read_token_file():
    with open(os.environ['DOMINO_TOKEN_FILE']) as f:
        return f.read()

@backoff.on_exception(backoff.expo, RetryException, max_time=600)
def submit_mc_wh_call(method, endpoint, data=None): 
    domino_user_jwt = read_token_file()
    headers = {
        'Content-Type': 'application/json',
        'accept': 'application/json',
        'Authorization': f'Bearer {domino_user_jwt}',
    }
    mc_api_host = os.environ.get('MC_API_HOST', 'mc-api.space.gsk.com')
    url = f'https://{mc_api_host}/{endpoint}'
    verify = '/mnt/imported/code/space-tech-util/ssl/full-bundle.crt'

    try:
        response = requests.request(method, url, headers=headers, data=data, verify=verify)
        response.raise_for_status()
    except HTTPError as err:
        if data:
            logger.error(f'Request Body: {data}')
        logger.error(f'Request Response: {response.text}')
        
        if response.status_code in retry_codes:
            raise RetryException(err)

        raise err

@backoff.on_exception(backoff.expo, RetryException, max_time=600)
def submit_api_call(method, endpoint, data=None):
    headers = {
        'Content-Type': 'application/json',
        'accept': 'application/json',
    }
    url = f'{DOMINO_API_HOST}/{endpoint}'

    try:
        response = requests.request(method, url, headers=headers, data=data)
        response.raise_for_status()
    except HTTPError as err:
        if data:
            logger.error(f'Request Body: {data}')
        logger.error(f'Request Response: {response.text}')
        
        if response.status_code in retry_codes:
            raise RetryException(err)

        raise err

    # Some API responses have JSON bodies, some are empty
    try:
        return response.json()
    except:
        try:
            return response.text
        except:
            return response


def get_job_status(job_id):
    endpoint = f'api/jobs/beta/jobs/{job_id}'
    method = 'GET'
    job_information = submit_api_call(method, endpoint)
    job_status = job_information['job']['status']['executionStatus']

    return job_status

def get_project_datasets():
    endpoint = f'api/datasetrw/v2/datasets?projectIdsToInclude={DOMINO_PROJECT_ID}'
    method = 'GET'
    project_datasets = submit_api_call(method, endpoint)

    return project_datasets


def take_dataset_snapshot(dataset_id):
    endpoint = f'api/datasetrw/v1/datasets/{dataset_id}/snapshots'
    method = 'POST'
    data = { "relativeFilePaths":["."] }
    snapshot_response = submit_api_call(method, endpoint, data=json.dumps(data))

    snapshot_id = snapshot_response['snapshot']['id']
    snapshot_timestamp = snapshot_response['snapshot']['createdAt']
    dt = datetime.strptime(snapshot_timestamp, '%Y-%m-%dT%H:%M:%S.%fZ')
    formatted_timestamp = str(dt.strftime('D%d-%b-%Y-T%H-%M-%S'))

    snapshot_status = ''
    while snapshot_status.lower() != 'active':
        sleep(2)
        endpoint = f'api/datasetrw/v1/snapshots/{snapshot_id}'
        method = 'GET'
        snapshot_status_response = submit_api_call(method, endpoint)
        snapshot_status = snapshot_status_response['snapshot']['status']

    return snapshot_id, formatted_timestamp, snapshot_response


def tag_dataset_snapshot(dataset_id, snapshot_id, formatted_timestamp):
    endpoint = f'api/datasetrw/v1/datasets/{dataset_id}/tags'
    method = 'POST'

    # First, apply the timestamp tag    
    tags = [ formatted_timestamp ]
    for tag in tags:
        data = { "snapshotId": snapshot_id, "tagName": tag }
        tag_response = submit_api_call(method, endpoint, data=json.dumps(data))

    # Second, apply the job tag and handle multiple tags in a run
    counter = 1
    tag_succeeded = False
    while not tag_succeeded:
        try:
            job_tag = f'JOB{DOMINO_RUN_ID}'
            tags = [ job_tag ]
            for tag in tags:
                data = { "snapshotId": snapshot_id, "tagName": tag }
                tag_response = submit_api_call(method, endpoint, data=json.dumps(data))
            tag_succeeded = True
        except HTTPError as err:
            if "already exists within dataset" in str(err.response):
                job_tag = f'JOB{DOMINO_RUN_ID}-{counter}'
                counter += 1
                if counter > 100:
                    raise err  # limit to 100 snapshots in the same run
            else:
                raise err


def format_snapshot_comment(snapshot_response, formatted_timestamp):
    snapshot_json = snapshot_response
    dataset_id = snapshot_json['snapshot']['datasetId']

    endpoint = f'api/datasetrw/v1/datasets/{dataset_id}'
    method = 'GET'
    dataset_response = submit_api_call(method, endpoint)

    dataset_name = dataset_response['dataset']['name']
    snapshot_comment = \
        f"Controlled execution results snapshot:\\\n\\\n \
            Dataset ID: {snapshot_json['snapshot']['datasetId']}\\\n \
            Dataset name: {dataset_name}\\\n \
            Author MUD ID: {DOMINO_STARTING_USERNAME}\\\n \
            Creation time: {formatted_timestamp}"

    return snapshot_comment


def format_env_vars_comment():
    variables_comment = 'Project environment variables:\\\n'
    for env_var in os.environ:
        if env_var.startswith('DMV'):
            variables_comment += f'\\\n{env_var}: {os.environ[env_var]}'
    
    return variables_comment


def leave_comment_on_job(comment_text):
    endpoint = f'v4/jobs/{DOMINO_RUN_ID}/comment'
    method = 'POST'
    data = { "comment": comment_text }
    comment_response = submit_api_call(method, endpoint, data=json.dumps(data))


def cleanup_dataset():
    if DOMINO_IS_GIT_BASED == 'true':
        dataset_root = '/mnt/data'
    else:
        dataset_root = '/domino/datasets/local'
    dataset_path = f'{dataset_root}/{DOMINO_PROJECT_NAME}'
    if os.path.exists(dataset_path):
        PROTECTED_DIR = 'inputdata'
        for (root, dirs, files) in os.walk(dataset_path, topdown=True):
            for name in files:
                if PROTECTED_DIR not in root:
                    os.remove(os.path.join(root, name))


def full_cx():
    project_datasets = get_project_datasets()
    for dataset in project_datasets['datasets']:
        # Only create snapshot for default dataset (dataset name matches project name)
        if dataset['dataset']['name'] == DOMINO_PROJECT_NAME:
            dataset_id = dataset['dataset']['id']
            snapshot_id, formatted_timestamp, snapshot_response = take_dataset_snapshot(dataset_id)
            tag_dataset_snapshot(dataset_id, snapshot_id, formatted_timestamp)
            snapshot_comment = format_snapshot_comment(snapshot_response, formatted_timestamp)
            leave_comment_on_job(snapshot_comment)

    variables_comment = format_env_vars_comment()
    leave_comment_on_job(variables_comment)

def copy_analysis_outputs():
    endpoint = 'wh/domino/v1/sd-copy'
    method = 'POST'
    data = {
        'sourceProjectId': os.environ['DOMINO_PROJECT_ID']
    }
    sd_copy_job = submit_mc_wh_call(method, endpoint, data=json.dumps(data))
    logger.info('Copy of analysis outputs to the METEOR Source Domain started')


def build_dag(cfg_file_path):
    c = configparser.ConfigParser(allow_no_value=False)
    c.read(cfg_file_path)
    tasks = {}
    dependency_graph = {}
    output_to_task =  {}  # map of output file name to task id; used to derive additional dependencies
    task_ids = c.sections()
    if len(task_ids) == 0:
        raise Exception("Empty config provided")
    for task_id in task_ids:
        if c.has_option(task_id, "depends"):
            dependencies_str = c.get(task_id, "depends")
            dependencies = [s.strip() for s in dependencies_str.split(',')]
        else:
            dependencies = []
        inputs = []
        if c.has_option(task_id, 'input'):
            inputs = [os.path.expandvars(s.strip()) for s in c.get(task_id, "input").split(',')]
            logger.debug(f"Read inputs for {task_id} as {inputs}")
        outputs = []
        if c.has_option(task_id, "output"):
            outputs = [os.path.expandvars(s.strip()) for s in c.get(task_id, "output").split(',')]
            for o in outputs:
                output_to_task[o] = task_id
        dependency_graph[task_id] = dependencies
        command_str = c.get(task_id, "command")
        command = str(command_str)
        domino_run_kwargs = {}
        if c.has_option(task_id, "max_retries"):
            max_retries = c.get(task_id, "max_retries")
            domino_run_kwargs["max_retries"] = max_retries
        if c.has_option(task_id, "tier"):
            tier = c.get(task_id, "tier")
            domino_run_kwargs["tier"] = tier
        # Set the desired compute environment
        if c.has_option(task_id, "environment"):
            environment = c.get(task_id, "environment")
            domino_run_kwargs["environment"] = environment
        if c.has_option(task_id, 'project_repo_git_ref'):
            project_repo_git_ref = c.get(task_id, 'project_repo_git_ref')
            domino_run_kwargs['project_repo_git_ref'] = project_repo_git_ref
        if c.has_option(task_id, 'imported_repo_git_refs'):
            imported_repo_git_refs = c.get(task_id, 'imported_repo_git_refs')
            domino_run_kwargs['imported_repo_git_refs'] = imported_repo_git_refs
        tasks[task_id] = DominoRun(task_id, command, inputs, outputs, **domino_run_kwargs)

    # add dependencies defined only by input-output file dependencies
    for task_id in task_ids:
        logger.debug(f"Check IO dependencies for {task_id}")
        if c.has_option(task_id, 'input'):
            inputs = tasks[task_id].inputs
            for i in inputs:
                logger.debug(f"Test if {i} is an output from another task")
                if i in output_to_task.keys():
                    dependent_task = output_to_task[i]
                    logger.debug(f"Found task {dependent_task} that produces output {i}")
                    if dependent_task not in dependency_graph[task_id]:
                        logger.info(f"Adding dependency: [{task_id}] depends: {dependent_task}")
                        dependency_graph[task_id].append(dependent_task)

    return Dag(tasks, dependency_graph)



class PipelineRunner:
    '''
    The PipelineRunner class is designed to manage and track the execution of tasks in a pipeline. 
    It is stateful, maintaining the run IDs and states of various tasks to support retry logic. 
    The state is stored in a Directed Acyclic Graph (DAG) object.

    Attributes:
    dag (DAG): The DAG object that stores the state of tasks.
    tick_freq (int): The number of seconds to wait between job submissions, default is 5.
    is_local (bool): A flag indicating if the pipeline is running in a local environment, default is False.
    queue_limit (int): The max number of jobs to run in parallel.
    '''

    def __init__(self, dag, tick_freq=5, is_local=False, queue_limit=10, add_timestamp=True, job_name="job1", job_title="title"):
        self.dag = dag
        self.tick_freq = tick_freq
        self.is_local = is_local
        self.queue_limit = queue_limit
        self.add_timestamp = add_timestamp
        self.job_name = job_name
        self.job_title = job_title

    def run(self):
        while True:
            if self.dag.pipeline_status() == 'Succeeded':
                break
            elif self.dag.pipeline_status() == 'Failed':
                raise Exception(f"Pipeline Execution Failed for tasks: {[str(t) for t in self.dag.get_failed_tasks()]}")
            if not self.is_local:
                # Suspend job submission until the "multijob_locked" project tag is removed
                while True:
                    jobs_locked = self.are_jobs_locked()
                    if jobs_locked == False:
                        break
                    time.sleep(self.tick_freq)
            # Suspend job submission until the number of jobs is below the queue limit
            message_logged = False
            while True:
                queued_job_count = self.check_queue_limit()
                if queued_job_count < self.queue_limit:
                    break
                if not message_logged:
                    logger.info('At limit for queued jobs, waiting for jobs to complete.')
                    message_logged = True
                time.sleep(max(self.tick_freq, 1))

            ready_tasks = self.dag.get_ready_tasks()
            if ready_tasks:
                logger.info("Ready tasks: {0}".format(", ".join([task.task_id for task in ready_tasks])))
            # Pull one task out of the ready queue, submit it, wait 1 tick, and repeat tag check
            for task in ready_tasks:
                self.submit_task(task)
                break
            time.sleep(self.tick_freq)


    def get_hardware_tier_id(self, hardware_tier_name):
        endpoint = f'v4/projects/{DOMINO_PROJECT_ID}/hardwareTiers'
        method = 'GET'
        available_hardware_tiers = submit_api_call(method, endpoint)
        for hardware_tier in available_hardware_tiers:
            if hardware_tier['hardwareTier']['name'] == hardware_tier_name:
                hardware_tier_id = hardware_tier['hardwareTier']['id']

        return hardware_tier_id


    def set_project_tag(self):
        endpoint = f'v4/projects/{DOMINO_PROJECT_ID}/tags'
        method = 'POST'
        data = {
            'tagNames': [
                'multijob_locked'
            ]
        }
        set_tag_response = submit_api_call(method, endpoint, data=json.dumps(data))
        tag_id = set_tag_response[0]['id']

        return tag_id


    def delete_project_tag(self, tag_id):
        endpoint = f'v4/projects/{DOMINO_PROJECT_ID}/tags/{tag_id}'
        method = 'DELETE'
        delete_tag_response = submit_api_call(method, endpoint)


    def are_jobs_locked(self):
        jobs_locked = False
        endpoint = f'v4/projects/{DOMINO_PROJECT_ID}'
        method = 'GET'
        project_summary = submit_api_call(method, endpoint)

        if 'tags' in project_summary:
            for tag in project_summary['tags']:
                if tag['name'] == 'multijob_locked':
                    jobs_locked = True
                    break
    
        return jobs_locked


    def check_queue_limit(self):
        if self.is_local:
            return self.dag.count_local_submitted_jobs()
        else:
            endpoint = f'api/jobs/beta/jobs?projectId={DOMINO_PROJECT_ID}&statusFilter=queued'
            method = 'GET'
            queued_jobs = submit_api_call(method, endpoint)

            queued_jobs_count = queued_jobs['metadata']['totalCount']

            return queued_jobs_count


    def get_imported_repos(self):
        endpoint = f'api/projects/v1/projects/{DOMINO_PROJECT_ID}/repositories'
        method = 'GET'
        imported_repos = submit_api_call(method, endpoint)
        
        return imported_repos

    # Imported repo configs are specified in 3 parts, delimited with commas.
    # The format is: repo_name,ref_type,ref_value
    # Multiple repo configs are delimited by spaces. For example:
    # imported_repo_git_refs: my-repo,branches,feature-branch other-repo,tags,tag-value

    # We need to store the user defined temporary config, as well as the config's starting state.
    # Builds 2 dicts, "original_config" from the current repo config pulled by the API,
    # and "temp_config" using the user-provided values in the .cfg file
    def build_imported_repo_configs(self, imported_repo_config):
        current_imported_repos = self.get_imported_repos()
        temp_config = { }
        original_config = { }
        # Multiple repos can be specified by delimiting entries with a space
        imported_repos_to_update = imported_repo_config.split(' ')
        for i, repo in enumerate(imported_repos_to_update, start=1):
            # Individual repo configs are delimited by commas. The first position is the repo name,
            # the second is the ref type, but there may not be a third, for example if the ref type is 'HEAD'.
            modified_repo_name, *modified_git_ref = repo.split(',')
            modified_ref_type = modified_git_ref[0]
            if len(modified_git_ref) == 2:
                modified_ref_value = modified_git_ref[1]
            for current_repo in current_imported_repos['repositories']:
                if current_repo['name'] == modified_repo_name:
                    temp_config[i] = {
                        'id': current_repo['id'],
                        'ref_type': modified_ref_type,
                    }
                    original_config[i] = {
                        'id': current_repo['id'],
                        'ref_type': current_repo['defaultRef']['refType']
                    }
                    if 'value' in current_repo['defaultRef']:
                        original_config[i]['ref_value'] = current_repo['defaultRef']['value']
                    if modified_ref_value is not None:
                        temp_config[i]['ref_value'] = modified_ref_value

        # The resulting dicts have the minimum required info to update the repo config for the project
        # and are formatted as such:
        # {
        #     '1': {
        #         'id': 'repo-uid',
        #         'ref_type': 'git-ref-type',
        #         'ref_value': 'value-of-that-ref'
        #     },
        #     '2': {
        #         'id': 'repo-uid',
        #         'ref_type': 'git-ref-type',
        #         'ref_value': 'value-of-that-ref'
        #     }
        # }
        return original_config, temp_config


    # Accepts the repo config dicts created by build_imported_repo_configs()
    def set_imported_repo_config(self, repo_config):
        for i in repo_config:
            repo_id = repo_config[i]['id']
            if repo_config[i]['ref_type'] == 'Head':
                ref_type = 'HEAD'
            else:
                ref_type = repo_config[i]['ref_type']
            if 'ref_value' in repo_config[i]:
                ref_value = repo_config[i]['ref_value']
            endpoint = f'v4/projects/{DOMINO_PROJECT_ID}/gitRepositories/{repo_id}/ref'
            method = 'PUT'
            git_ref_config = {
                'type': ref_type,
            }
            if 'ref_value' in repo_config[i]:
                git_ref_config['value'] = ref_value

            response = submit_api_call(method, endpoint, data=json.dumps(git_ref_config))


    def submit_task(self, task):
        logger.info(f"## Submitting task ## task_id: {task.task_id}, command: {task.command}, tier override: {task.tier}, environment override: {task.environment}, main repo override: {task.project_repo_git_ref}, imported repo overrides: {task.imported_repo_git_refs}")
        request_body = {
            'projectId': DOMINO_PROJECT_ID,
            'title': task.task_id
        }

        if DOMINO_IS_GIT_BASED == 'true':
            dataset_root = '/mnt/data'
        else:
            dataset_root = '/domino/datasets/local'
        if os.path.exists(f'{dataset_root}/{DOMINO_PROJECT_NAME}'):
            log_path = f'{dataset_root}/{DOMINO_PROJECT_NAME}/logs'
            if not os.path.exists(log_path):
                os.makedirs(log_path)
        
        program_name = extract_program_name(task.command)
        if program_name.lower().endswith('.r'):
            logger.info('R script detected. Running via logrx::axecute().')
            task.command = f'R -e "tryCatch(expr={{logrx::axecute(\'{task.command}\',log_path=\'{log_path}\')}},error=function(e){{source(\'{task.command}\')}})"'
        elif program_name.lower().endswith('.sas') and not task.command.startswith('sas '):
            logger.info('SAS script detected. Running via sas.')
            task.command = f'sas {task.command}'
            task.success_codes = (0, 1)  # don't flag SAS warnings as failures
        elif program_name.lower().endswith('.py') and not task.command.startswith('python'):  # could be python or python3
            logger.info('Python script detected. Running via python3.')
            task.command = f'python3 {task.command}'
        elif program_name.lower().endswith('.js') and not task.command.startswith('node '):
            logger.info('JavaScript detected. Running via node.')
            task.command = f'node {task.command}'

        if self.is_local:
            task.set_status('Submitted')
            out_log_path = f"{log_path}/{task.task_id}_out.txt"
            err_log_path = f"{log_path}/{task.task_id}_err.txt"
            task.process, task.out_thread, task.err_thread = start_background_job(task.task_id,
                                                                                  task.command,
                                                                                  out_log_path,
                                                                                  err_log_path,
                                                                                  self.add_timestamp)
        else:
            logger.info(f'Launching job for command: {task.command}')
            request_body['runCommand'] = task.command
            # If the user has specified custom git refs, set the "multijob_locked" tag before doing anything else
            # Then, save the current imported repo config to revert later before setting the user config.
            if task.imported_repo_git_refs:
                tag_id = self.set_project_tag()
                original_config, temp_config = self.build_imported_repo_configs(task.imported_repo_git_refs)
                self.set_imported_repo_config(temp_config)
            if task.tier:
                hardware_tier_id = self.get_hardware_tier_id(task.tier)
                request_body['hardwareTier'] = hardware_tier_id
            if task.environment:
                request_body['environmentId'] = task.environment
            if task.project_repo_git_ref:
                project_repo_config = task.project_repo_git_ref.split(',')
                if len(project_repo_config) == 2:
                    request_body['mainRepoGitRef'] = { 'refType': project_repo_config[0], 'value': project_repo_config[1] }
                else:    
                    request_body['mainRepoGitRef'] = { 'refType': project_repo_config[0] }

            endpoint = 'api/jobs/v1/jobs'
            method = 'POST'
            job_info = submit_api_call(method, endpoint, data=json.dumps(request_body))
            logger.info(job_info)
            # Domino doesn't load the imported git repo config as part of the job submission.
            # Instead, it's loaded during job startup, which is the 'Preparing' state.
            # If using a custom git ref, Multijob should block until that job is Preparing.
            # Once the job is starting up, revert the git config and delete the "multijob_lock" tag.
            if task.imported_repo_git_refs:
                while True:
                    job_state = get_job_status(job_info['job']['id'])
                    if job_state not in ['Queued', 'Pending']:
                        break
                    time.sleep(3)
                self.set_imported_repo_config(original_config)
                self.delete_project_tag(tag_id)

            logger.info("## Submitted task: {0} ##".format(task.task_id))
            task.job_id = job_info['job']['id']
            task.set_status('Submitted') # will technically be Queued or something else, but this will update on the next status check

            # add multijob tags to the job for reporting
            set_job_tag(DOMINO_PROJECT_ID, task.job_id, "muiltijob")
            set_job_tag(DOMINO_PROJECT_ID, task.job_id, self.job_name)
            set_job_tag(DOMINO_PROJECT_ID, task.job_id, "title:" + self.job_title)
        
 
def set_job_tag(project_id, job_id, tag):
    tag_endpoint = f"v4/jobs/{job_id}/tag"
    tag_method = 'POST'
    tag_payload = {
        "tagName": tag,
        "projectId": project_id
    }
    tag_info = submit_api_call(tag_method, tag_endpoint, data=json.dumps(tag_payload))

"""
Parse command line arguments.  Read and validate the configuration file and initiate jobs.
"""

def main():
    global KEEP_EMPTY_LOGS, FORCE_RERUN
    parser = ArgumentParser(description="Run a directed, acyclic graph of jobs defined in a config file.",
                            epilog="Multijob is designed to run only within a Domino workspace.")
    parser.add_argument('config_path',
                        type=str,
                        help='path to the config file defining the multijob DAG')
    parser.add_argument('-j',
                        required=False,
                        type=int,
                        default=1,
                        metavar='N',
                        help='number of jobs to run in parallel for local jobs or the maximum number of jobs in queued state for Domino Jobs (default: 1 for local, 10 for Domino Jobs)')
    parser.add_argument('-l', '--local',
                        action='store_true',
                        help='if provided, jobs run locally rather than being launched as Domino Jobs (default: launch jobs)')
    parser.add_argument('--nots',
                        action='store_true',
                        help="if provided, do not add timestamps to output and error logs (default: add timestamps). "
                        "Note that this only applies to local job runs and doesn't apply if your job manages its own log, such as sas.")
    parser.add_argument('-k', '--keep',
                        action='store_true',
                        help='if provided, keep output and error logs even if empty (default: false)')
    parser.add_argument('-f', '--force',
                        action='store_true',
                        help='if provided, force the run of dependent jobs (default: false). Overrides input/output file timestamp checks.')

    args = parser.parse_args()

    cli_add_timestamp = not args.nots
    KEEP_EMPTY_LOGS = args.keep
    FORCE_RERUN = args.force

    tick_freq = 0 if args.local else 5
    queue_limit = max(1, min(args.j, 128 if args.local else 10))

    job_start_time = re.sub("[-.:]", "", str(datetime.now())).replace(" ", "")[0:14]
    job_name = f"name:{DOMINO_PROJECT_OWNER}:{DOMINO_PROJECT_NAME}:{job_start_time}"

    pipeline_cfg_path = args.config_path
    if os.path.exists(pipeline_cfg_path):
        if PRERUN_CLEANUP == 'true':
            cleanup_dataset()
        
        try:
            dag = build_dag(pipeline_cfg_path)
            logger.info(f"DAG: {dag}")
            dag.validate_dag()
            pipeline_runner = PipelineRunner(dag,
                                             tick_freq=tick_freq,
                                             is_local=args.local,
                                             queue_limit=queue_limit,
                                             add_timestamp=cli_add_timestamp,
                                             job_name=job_name,
                                             job_title=pipeline_cfg_path)
            pipeline_runner.run()
            if CXRUN == 'true':
                full_cx()
                """ 
                Removing call to copy_analysis_outputs which copies the default analysis dataset to the source domain 
                This due to a combination of issues. 
                1. Errors occur when a collaborator runs multi-job. 
                2. Some users are setting the CX project's DMV_ISCX to false which generates an error as this function is incorrectly processed
                when the project id is CX (only analysis project ids are expected)
                Business re-evaluating the requirements for the copy to the meteor SD before proceeding.
                else:
                    copy_analysis_outputs()
                """
        except KeyboardInterrupt:
            sys.exit(1)
        except configparser.DuplicateSectionError as e:
            logger.error(e)
            sys.exit(1)
        except HTTPError as err:
            logger.exception(err)
            sys.exit(1)
    else:
        logger.error(f"Empty or missing config file {pipeline_cfg_path}")
        sys.exit(1)


if __name__ == '__main__':
    main()
