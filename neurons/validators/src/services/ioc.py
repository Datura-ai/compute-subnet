import asyncio

from services.docker_service import DockerService
from services.miner_service import MinerService
from services.ssh_service import SSHService
from services.task_service import TaskService
from services.redis_service import RedisService

ioc = {}


async def initiate_services():
    ioc["SSHService"] = SSHService()
    ioc["RedisService"] = RedisService()
    ioc["TaskService"] = TaskService(
        ssh_service=ioc["SSHService"],
        redis_service=ioc["RedisService"]
    )
    ioc["DockerService"] = DockerService(
        ssh_service=ioc["SSHService"],
        redis_service=ioc["RedisService"]
    )
    ioc["MinerService"] = MinerService(
        ssh_service=ioc["SSHService"],
        task_service=ioc["TaskService"],
        docker_service=ioc["DockerService"],
        redis_service=ioc["RedisService"]
    )


def sync_initiate():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(initiate_services())


sync_initiate()
