from flux.job import JobspecV1
import os
import json

from typing import Optional
from dataclasses import dataclass, fields
from ams import util
from ams.store import AMSDataStore 
from ams.rmq import AMSRMQConfiguration
from typing import Dict, List, Union, Optional, Mapping
from pathlib import Path


def constuct_cli_cmd(executable, *args, **kwargs):
    command = [executable]
    for k, v in kwargs.items():
        command.append(str(k))
        command.append(str(v))

    for a in args:
        command.append(str(a))

    return command


@dataclass(kw_only=True)
class AMSJobResources:
    nodes: int
    tasks_per_node: int
    cores_per_task: int = 1
    exclusive: Optional[bool] = True
    gpus_per_task: Optional[int] = 0

    def to_dict(self):
        return {field.name: getattr(self, field.name) for field in fields(self)}


class AMSJob:
    """
    Class Modeling a Job scheduled by AMS. This is a convenience layer on top of a flux JobspecV1
    and provides less features than the flux one. We use this abstraction to describe the job specification
    in the json file.
    """

    @classmethod
    def generate_formatting(cls, store):
        return {"AMS_STORE_PATH": store.root_path}

    def __init__(
        self,
        name: str,
        executable: str,
        environ: Optional[Mapping[str, str]] = {},
        resources: Optional[AMSJobResources]=None,
        stdout: Optional[str]=None,
        stderr: Optional[str]=None,
        ams_log: bool=False,
        is_mpi: bool=False,
        cli_args: Optional[List[str]]=[],
        cli_kwargs: Optional[Dict[str,str]]={},
    ):
        """Attaches a callable that will be called when the future finishes.

        :param name: An arbitary name for every job. This can be an arbitary string. 
        :param executable: A string pointing to the executable to be executed
        :param environ: The environment to be used  when scheduling the job. 
        :param resources: The resources dedicated to this job.
        :param stdout File to redirect the stdout.
        :param stderr File to redirect the stderr.
        :param ams_log: A boolean value to enable the logging of AMS printouts
        :param is_mpi: Whether the job is an mpi job.
        :param cli_args: positional arguments of the cli command 
        :param cli_kwargs: key-word arguments of the cli command 
        :return: ``self``
        """

        self._name = name
        self._executable = executable
        self._resources = resources
        if isinstance(self._resources, dict):
            self._resources = AMSJobResources(**resources)

        self.environ = environ
        self._stdout = stdout
        self._stderr = stderr
        self._cli_args = []
        self._cli_kwargs = {}
        self._is_mpi = is_mpi
        self._ams_log = ams_log
        if cli_args is not None:
            self._cli_args = list(cli_args)
        if cli_kwargs is not None:
            self._cli_kwargs = dict(cli_kwargs)

    def generate_cli_command(self):
        return constuct_cli_cmd(self.executable, *self._cli_args, **self._cli_kwargs)

    def __str__(self):
        data = {}
        data["name"] = self._name
        data["executable"] = self._executable
        data["stdout"] = self._stdout
        data["stderr"] = self._stderr
        data["cli_args"] = self._cli_args
        data["cli_kwargs"] = self._cli_kwargs
        data["resources"] = self._resources
        return f"{self.__class__.__name__}\nCLI:{' '.join(self.generate_cli_command())}\nJOB-Descr:{data}"

    def precede_deploy(self, store, rmq=None):
        """
        Will be called by the ams job scheduler just before submitting the job. If there is some modification 
        required to the submission environment a child class can override this method and do the modification.
        """
        pass

    @property
    def resources(self):
        """The resources property."""
        return self._resources

    @resources.setter
    def resources(self, value):
        self._resources = value

    @property
    def executable(self):
        """The executable property."""
        return self._executable

    @executable.setter
    def executable(self, value):
        self._executable = value

    @property
    def environ(self):
        """The environ property."""
        return self._environ

    @environ.setter
    def environ(self, value):
        if isinstance(value, type(os.environ)):
            self._environ = dict(value)
            return
        elif not isinstance(value, dict) and value is not None:
            raise RuntimeError(f"Unknwon type {type(value)} to set job environment")

        self._environ = value

    @property
    def stdout(self):
        """The stdout property."""
        return self._stdout

    @stdout.setter
    def stdout(self, value):
        self._stdout = value

    @property
    def stderr(self):
        """The stderr property."""
        return self._stderr

    @stderr.setter
    def stderr(self, value):
        self._stderr = value

    @property
    def name(self):
        """The name property."""
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @classmethod
    def from_dict(cls, _dict):
        return cls(**_dict)

    def to_dict(self):
        data = {}
        data["name"] = self._name
        data["executable"] = self._executable
        data["stdout"] = self._stdout
        data["stderr"] = self._stderr
        data["cli_args"] = self._cli_args
        data["cli_kwargs"] = self._cli_kwargs
        data["resources"] = self._resources.to_dict()
        return data

    def to_flux_jobspec(self):
        jobspec = JobspecV1.from_command(
            command=self.generate_cli_command(),
            num_tasks=self.resources.tasks_per_node * self.resources.nodes,
            num_nodes=self.resources.nodes,
            cores_per_task=self.resources.cores_per_task,
            gpus_per_task=self.resources.gpus_per_task,
            exclusive=self.resources.exclusive,
        )

        if self._is_mpi is not None:
            print("Setting MPI and spectrum")
            jobspec.setattr_shell_option("mpi", "spectrum")
        if self.resources.gpus_per_task is not None:
            jobspec.setattr_shell_option("gpu-affinity", "per-task")
        jobspec.stdout = self.stdout
        jobspec.stderr = self.stderr
        if self._stdout is None:
            jobspec.stdout = "ams_test.out"
        if self._stderr is None:
            jobspec.stderr = "ams_test.err"

        jobspec.environment = dict(self.environ)
        jobspec.cwd = os.getcwd()

        return jobspec


class AMSDomainJob(AMSJob):
    """
    The ``AMSDomainJob`` represents a job executing the original physics code that should be linked in with ``AMSlib``.
    ``AMSDomainJob`` modifies the environment of the executing job just before submission using the ``precede_deploy`` hook. 
    """
    def _generate_ams_objects_store(self, store, rmq):
        '''
        Generates the dictionary requirements of the ``AMSlib`` database description. 

        :param store: The AMSDataStore that contains all the files and directories of the AMS database. 
        :param rmq: The AMSRMQConfiguration containing all required information to connect to the RMQ server

        :return: A dictionary with the correct structure
        '''

        ams_object = dict()
        if rmq is None:
            if self.stage_dir is None:
                ams_object["db"] = {"fs_path": str(store.get_candidate_path()), "dbType": "hdf5"}
            else:
                ams_object["db"] = {"fs_path": self.stage_dir, "dbType": "hdf5"}
        else:
            ams_object["db"] = {"rmq_config": rmq.to_dict(AMSlib=True), "dbType": "rmq", "update_surrogate": False}
        return ams_object

    def _generate_ams_object(self, store: AMSDataStore, rmq: Optional[AMSRMQConfiguration]=None):
        '''
        Generates a ``AMS_OBJECTS`` dictionary and adding the appropriate 'database', ml_models and domain_models fields required by the application. 

        :param store: The AMSDataStore that contains all the files and directories of the AMS database. 
        :param rmq: The AMSRMQConfiguration containing all required information to connect to the RMQ server

        :return: A dictionary with the correct structure
        '''
        ams_object = self._generate_ams_objects_store(store, rmq) 

        ams_object["ml_models"] = dict()
        ams_object["domain_models"] = dict()

        for i, name in enumerate(self.domain_names):
            models = store.search(domain_name=name, entry="models", version="latest")
            print(json.dumps(models, indent=6))
            # This is the case in which we do not have any model
            # Thus we create a data gathering entry
            if len(models) == 0:
                model_entry = {
                    "uq_type": "random",
                    "model_path": "",
                    "uq_aggregate": "mean",
                    "threshold": 1,
                    "db_label": name,
                }
            else:
                model = models[0]
                model_entry = {
                    "uq_type": model["uq_type"],
                    "model_path": model["file"],
                    "uq_aggregate": "mean",
                    "threshold": model["threshold"],
                    "db_label": name,
                }

            ams_object["ml_models"][f"model_{i}"] = model_entry
            ams_object["domain_models"][name] = f"model_{i}"
        return ams_object

    def __init__(self, domain_names, stage_dir, *args, **kwargs):
        self._domain_names = domain_names
        self.stage_dir = stage_dir
        self._ams_object = None
        self._ams_object_fn = None
        super().__init__(*args, **kwargs)

    @property
    def domain_names(self):
        """The domain_names property."""
        return self._domain_names

    @domain_names.setter
    def domain_names(self, value):
        self._domain_names = value

    @classmethod
    def from_descr(cls, descr, stage_dir=None):
        domain_job_resources = AMSJobResources(**descr["resources"])
        return cls(
            name=descr["name"],
            stage_dir=stage_dir,
            domain_names=descr["domain_names"],
            environ=os.environ,
            resources=domain_job_resources,
            ams_log=descr["ams_log"] if "ams_log" in descr else False,
            **descr["cli"],
        )

    def precede_deploy(self, store, rmq=None):
        '''
        Generates a ``AMS_OBJECTS`` json file and adding the appropriate 'database', ml_models and domain_models fields required by the application
        and if requested also adds the AMS verbosity level to the environment

        :param store: The AMSDataStore that contains all the files and directories of the AMS database. 
        :param rmq: The AMSRMQConfiguration containing all required information to connect to the RMQ server

        :return: A dictionary with the correct structure
        '''

        self._ams_object = self._generate_ams_object(store, rmq)
        tmp_path = util.mkdir(store.root_path, "tmp")
        # NOTE: THere is a big assumption here that the job-to be submitted has access to this tmp path
        # currently we place it under tmp_path which is under the AMSDataStore directory.
        self._ams_object_fn = f"{tmp_path}/{util.get_unique_fn()}.json"
        with open(self._ams_object_fn, "w") as fd:
            json.dump(self._ams_object, fd)

        self.environ["AMS_OBJECTS"] = str(self._ams_object_fn)
        if self._ams_log:
            print("Setting log level")
            self.environ["AMS_LOG_LEVEL"] = "debug"


class AMSMLJob(AMSJob):
    def __init__(self, domain, *args, **kwargs):
        '''
        A AMSJob training or performing sub-selection. This is a class mainly representing a team 
        of ML experts which will provide to the infrastructure the appropriate models. ML jobs are
        associcated with a ``domain`` pointing which domain those will train.
        '''
        self._domain = domain
        super().__init__(*args, **kwargs)

    @property
    def domain(self):
        """The domain property."""
        return self._domain

    @domain.setter
    def domain(self, value):
        self._domain = value

    @classmethod
    def from_descr(cls, store, descr):
        formatting = AMSJob.generate_formatting(store)
        resources = AMSJobResources(**descr["resources"])
        cli_kwargs = descr["cli"].get("cli_kwargs", None)
        if cli_kwargs is not None:
            for k, v in cli_kwargs.items():
                if isinstance(v, str):
                    cli_kwargs[k] = v.format(**formatting)
        cli_args = descr["cli"].get("cli_args", None)
        if cli_args is not None:
            for i, v in enumerate(cli_args):
                cli_args[i] = v.format(**formatting)

        return cls(
            descr["domain_name"],
            name=descr["name"],
            environ=None,
            stdout=descr["cli"].get("stdout", None),
            stderr=descr["cli"].get("stderr", None),
            executable=descr["cli"]["executable"],
            resources=resources,
            cli_kwargs=cli_kwargs,
            cli_args=cli_args,
            ams_log=descr["ams_log"] if "ams_log" in descr else False,
        )


class AMSMLTrainJob(AMSMLJob):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class AMSSubSelectJob(AMSMLJob):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class AMSStageJob(AMSJob):
    """
    A Job description for stating data from the application to the database. This class is internal
    and should be either inheritted by ``AMSFSTempStageJob`` or ``AMSNetworkStageJob``
    """
    def __init__(
        self,
        resources: Union[Dict[str, Union[str, int]], AMSJobResources],
        dest: str,
        persistent_db_path: str,
        store: bool = True,
        db_type: str = "dhdf5",
        policy: str = "process",
        prune_module_path: Optional[str] = None,
        prune_class: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        cli_args: List[str] = [],
        cli_kwargs: Mapping[str, str] = {},
    ):
        _cli_args = list(cli_args)
        if store:
            _cli_args.append("--store")
        else:
            _cli_args.append("--no-store")

        _cli_kwargs = dict(cli_kwargs)
        _cli_kwargs["--dest"] = dest
        _cli_kwargs["--persistent-db-path"] = persistent_db_path
        _cli_kwargs["--db-type"] = db_type
        _cli_kwargs["--policy"] = policy

        if prune_module_path is not None:
            assert Path(prune_module_path).exists(), "Module path to user pruner does not exist"
            assert prune_class is not None, "When defining a pruning module please define the class"
            _cli_kwargs["--load"] = prune_module_path
            _cli_kwargs["--class"] = prune_class

        super().__init__(
            name="AMSStageJob",
            executable="AMSDBStage",
            environ=environ,
            resources=resources,
            stdout=stdout,
            stderr=stderr,
            cli_args=_cli_args,
            cli_kwargs=_cli_kwargs,
        )


class AMSFSStageJob(AMSStageJob):
    """
    A job description for moving data from the application to the database reading the data from the filessytem.
    """

    def __init__(
        self,
        resources: Union[Dict[str, Union[str, int]], AMSJobResources],
        dest: str,
        persistent_db_path: str,
        src: str,
        store: bool = True,
        db_type="dhf5",
        pattern="*.h5",
        src_type: str = "shdf5",
        prune_module_path: Optional[str] = None,
        prune_class: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        cli_args: List[str] = [],
        cli_kwargs: Mapping[str, str] = {},
    ):

        _cli_args = list(cli_args)
        _cli_kwargs = dict(cli_kwargs)
        _cli_kwargs["--src"] = src
        _cli_kwargs["--src-type"] = src_type
        _cli_kwargs["--pattern"] = pattern
        _cli_kwargs["--mechanism"] = "fs"

        super().__init__(
            resources,
            dest,
            persistent_db_path,
            store,
            db_type,
            environ=environ,
            stdout=stdout,
            stderr=stderr,
            prune_module_path=prune_module_path,
            prune_class=prune_class,
            cli_args=_cli_args,
            cli_kwargs=_cli_kwargs,
        )


class AMSNetworkStageJob(AMSStageJob):
    """
    A job description for transfering data from the application to the database reading using rmq server-client protocol.
    This class represents the consumer part of the transactions.
    """
    def __init__(
        self,
        resources: Union[Dict[str, Union[str, int]], AMSJobResources],
        dest: str,
        persistent_db_path: str,
        creds: str,
        store: bool = True,
        db_type: str = "dhdf5",
        update_models: bool = False,
        prune_module_path: Optional[str] = None,
        prune_class: Optional[str] = None,
        environ: Optional[Mapping[str, str]] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        cli_args: List[str] = [],
        cli_kwargs: Mapping[str, str] = {},
    ):
        _cli_args = list(cli_args)
        if update_models:
            _cli_args.append("--update-rmq-models")
        _cli_kwargs = dict(cli_kwargs)
        _cli_kwargs["--creds"] = creds
        _cli_kwargs["--mechanism"] = "network"

        super().__init__(
            resources,
            dest,
            persistent_db_path,
            store,
            db_type,
            environ=environ,
            stdout=stdout,
            stderr=stderr,
            prune_module_path=prune_module_path,
            prune_class=prune_class,
            cli_args=_cli_args,
            cli_kwargs=_cli_kwargs,
        )

    @classmethod
    def from_descr(cls, descr, dest, persistent_db_path, creds, resources):
        return cls(resources, dest, persistent_db_path, creds, **descr)


class AMSFSTempStageJob(AMSJob):
    def __init__(
        self,
        store_dir,
        src_dir,
        dest_dir,
        resources,
        environ=None,
        stdout=None,
        stderr=None,
        prune_module_path=None,
        prune_class=None,
        cli_args=[],
        cli_kwargs={},
    ):
        _cli_args = list(cli_args)
        _cli_args.append("--store")
        _cli_kwargs = dict(cli_kwargs)
        _cli_kwargs["--dest"] = dest_dir
        _cli_kwargs["--src"] = src_dir
        _cli_kwargs["--pattern"] = "*.h5"
        _cli_kwargs["--db-type"] = "dhdf5"
        _cli_kwargs["--mechanism"] = "fs"
        _cli_kwargs["--policy"] = "process"
        _cli_kwargs["--persistent-db-path"] = store_dir
        _cli_kwargs["--src"] = src_dir

        if prune_module_path is not None:
            assert Path(prune_module_path).exists(), "Module path to user pruner does not exist"
            _cli_kwargs["--load"] = prune_module_path
            _cli_kwargs["--class"] = prune_class

        super().__init__(
            name="AMSStage",
            executable="AMSDBStage",
            environ=environ,
            resources=resources,
            stdout=stdout,
            stderr=stderr,
            cli_args=_cli_args,
            cli_kwargs=_cli_kwargs,
        )

    @staticmethod
    def resources_from_domain_job(domain_job):
        return AMSJobResources(
            nodes=domain_job.resources.nodes,
            tasks_per_node=1,
            cores_per_task=5,
            exclusive=False,
            gpus_per_task=domain_job.resources.gpus_per_task,
        )


class AMSOrchestratorJob(AMSJob):
    """
    A JOB to be scheduled "somewhere" that can schedule jobs "somewhere" else. Currently this is tested only when 
    the orchestrator schedules jobs within the same job-allocation
    """
    def __init__(self, flux_uri, rmq_config):
        super().__init__(
            name="AMSOrchestrator",
            executable="AMSOrchestrator",
            stdout="AMSOrchestrator-log.out",
            stderr="AMSOrchestrator-log.err",
            environ=os.environ,
            cli_kwargs={"--ml-uri": flux_uri, "--ams-rmq-config": rmq_config},
            # NOTE: Not sure about cores_per_task
            resources=AMSJobResources(nodes=1, tasks_per_node=1, cores_per_task=1, exclusive=False, gpus_per_task=0),
        )


def nested_instance_job_descr(num_nodes, cores_per_node, gpus_per_node, time="inf", stdout=None, stderr=None):
    """
    Create a nested job partion. This is useful to split resources among dedicated parts of an initial root allocation.
    Effectively the command creates a partition that sleeps indefinetely.
    """
    jobspec = JobspecV1.from_nest_command(
        command=["sleep", time],
        num_slots=num_nodes,
        num_nodes=num_nodes,
        cores_per_slot=cores_per_node,
        gpus_per_slot=gpus_per_node,
        # NOTE: This is set to true, cause we do not want the parent partion to
        # schedule other jobs to the same resources and allow the "partion" to
        # have exclusive ownership of the resources. We should rethink this,
        # as it may make the system harder to debug
        exclusive=True,
    )

    if stdout is not None:
        jobspec.stdout = stdout
    if stderr is not None:
        jobspec.stderr = stderr
    jobspec.cwd = os.getcwd()
    jobspec.environment = dict(os.environ)
    return jobspec


def get_echo_job(message):
    jobspec = JobspecV1.from_command(
        command=["echo", message], num_tasks=1, num_nodes=1, cores_per_task=1, gpus_per_task=0, exclusive=True
    )
    return jobspec
