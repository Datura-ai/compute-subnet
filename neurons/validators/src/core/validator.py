import asyncio
import json
import os
from payload_models.payloads import MinerJobRequestPayload

from core.config import settings
from core.utils import _m, get_extra_info, get_logger
from clients.subtensor_client import SubtensorClient
from services.docker_service import DockerService
from services.file_encrypt_service import FileEncryptService
from services.miner_service import MinerService
from services.redis_service import PENDING_PODS_PREFIX, RedisService
from services.ssh_service import SSHService
from services.task_service import TaskService, JobResult
from services.matrix_validation_service import ValidationService
from services.collateral_contract_service import CollateralContractService
from services.const import GPU_MODEL_RATES, JOB_TIME_OUT


logger = get_logger(__name__)

SYNC_CYCLE = 12
WEIGHT_MAX_COUNTER = 6
MINER_SCORES_KEY = "miner_scores"


class Validator:
    def __init__(self):

        self.should_exit = False
        self.last_job_run_blocks = 0
        self.default_extra = {}

        self.miner_scores = {}

    async def initiate_services(self):
        # initiate subtensor client
        self.subtensor_client = SubtensorClient.get_instance()

        ssh_service = SSHService()
        self.redis_service = RedisService()
        self.file_encrypt_service = FileEncryptService(ssh_service=ssh_service)
        self.validation_service = ValidationService()
        self.collateral_contract_service = CollateralContractService()
        task_service = TaskService(
            ssh_service=ssh_service,
            redis_service=self.redis_service,
            validation_service=self.validation_service,
            collateral_contract_service=self.collateral_contract_service
        )
        self.docker_service = DockerService(
            ssh_service=ssh_service,
            redis_service=self.redis_service,
        )
        self.miner_service = MinerService(
            ssh_service=ssh_service,
            task_service=task_service,
            redis_service=self.redis_service,
        )

        # init miner_scores
        try:
            if await self.subtensor_client.should_set_weights():
                self.miner_scores = {}
            else:
                miner_scores_json = await self.redis_service.get(MINER_SCORES_KEY)
                if miner_scores_json is None:
                    logger.info(
                        _m(
                            "[initiate_services] No data found in Redis for MINER_SCORES_KEY, initializing empty miner_scores.",
                            extra=get_extra_info(self.default_extra),
                        ),
                    )
                    self.miner_scores = {}
                else:
                    self.miner_scores = json.loads(miner_scores_json)

            # remove pod renting-in-progress status
            await self.redis_service.delete(PENDING_PODS_PREFIX)
        except Exception as e:
            logger.error(
                _m(
                    "[initiate_services] Failed to initialize miner_scores",
                    extra=get_extra_info({**self.default_extra, "error": str(e)}),
                ),
            )
            self.miner_scores = {}

        logger.info(
            _m(
                "[initiate_services] miner scores",
                extra=get_extra_info(
                    {
                        **self.default_extra,
                        **self.miner_scores,
                    }
                ),
            ),
        )

    async def calc_job_score(self, total_gpu_model_count_map: dict, job_result: JobResult):
        if job_result.score == 0:
            logger.info(
                _m(
                    "Debug: No need to calc score, score is 0",
                    extra=get_extra_info({
                        "executor_id": str(job_result.executor_info.uuid),
                        "job_batch_id": job_result.job_batch_id,
                        "gpu_model": job_result.gpu_model,
                        "gpu_count": job_result.gpu_count,
                        "score": 0,
                        "sysbox_runtime": job_result.sysbox_runtime,
                    })
                )
            )
            return 0

        gpu_model_rate = GPU_MODEL_RATES.get(job_result.gpu_model, 0)
        total_gpu_count = total_gpu_model_count_map.get(job_result.gpu_model, 0)

        if total_gpu_count == 0:
            return 0

        revenue_per_gpu_type = await self.redis_service.get_revenue_per_gpu_type(job_result.gpu_model)
        score_portion = gpu_model_rate + settings.TIME_DELTA_FOR_EMISSION * (revenue_per_gpu_type - gpu_model_rate)
        score = score_portion * job_result.gpu_count / total_gpu_count

        # get uptime of the executor
        uptime_in_minutes = await self.redis_service.get_executor_uptime(job_result.executor_info)
        # if uptime in the subnet is exceed 5 days, then it'll get max score
        # if uptime is less than 5 days, then it'll get score based on the uptime
        # give 50% of max still to avoid 0 score all miners at deployment
        # If sysbox_runtime is true, then the score will be increased by PORTION_FOR_SYSBOX per cent.
        five_days_in_minutes = 60 * 24 * 5
        score = score * (settings.PORTION_FOR_UPTIME + min((1 - settings.PORTION_FOR_UPTIME), uptime_in_minutes / five_days_in_minutes))

        if job_result.sysbox_runtime:
            score = score * (1 + settings.PORTION_FOR_SYSBOX)

        logger.info(
            _m(
                "Debug: calculating score",
                extra=get_extra_info({
                    "executor_id": str(job_result.executor_info.uuid),
                    "job_batch_id": job_result.job_batch_id,
                    "gpu_model": job_result.gpu_model,
                    "total_gpu_count": total_gpu_count,
                    "gpu_count": job_result.gpu_count,
                    "gpu_model_rate": gpu_model_rate,
                    "revenue_per_gpu_type": revenue_per_gpu_type,
                    "score_portion": score_portion,
                    "score": score,
                    "sysbox_runtime": job_result.sysbox_runtime,
                })
            )
        )

        return score

    async def sync(self):
        try:
            logger.info(
                _m(
                    "[sync] Syncing at subtensor",
                    extra=get_extra_info(self.default_extra),
                ),
            )

            # fetch miners
            miners = self.subtensor_client.get_miners()

            try:
                if await self.subtensor_client.should_set_weights():
                    await self.subtensor_client.set_weights(miners=miners, miner_scores=self.miner_scores)
            except Exception as e:
                logger.error(
                    _m(
                        "[sync] Error setting weights",
                        extra=get_extra_info(
                            {
                                **self.default_extra,
                                "error": str(e),
                            }
                        ),
                    ),
                )

            current_block = self.subtensor_client.get_current_block()
            logger.info(
                _m(
                    "[sync] Current block",
                    extra=get_extra_info(
                        {
                            **self.default_extra,
                            "current_block": current_block,
                        }
                    ),
                ),
            )

            if current_block - self.last_job_run_blocks >= settings.BLOCKS_FOR_JOB:
                job_block = (current_block // settings.BLOCKS_FOR_JOB) * settings.BLOCKS_FOR_JOB
                job_batch_id = await self.subtensor_client.get_time_from_block(job_block)

                miners = [miner for miner in miners if miner.axon_info.is_serving]

                logger.info(
                    _m(
                        "[sync] Send jobs to miners",
                        extra=get_extra_info(
                            {
                                **self.default_extra,
                                "miners": len(miners),
                                "current_block": current_block,
                                "job_batch_id": job_batch_id,
                            }
                        ),
                    ),
                )

                self.last_job_run_blocks = current_block

                encrypted_files = self.file_encrypt_service.ecrypt_miner_job_files()

                task_info = {}

                # request jobs
                jobs = [
                    asyncio.create_task(
                        self.miner_service.request_job_to_miner(
                            payload=MinerJobRequestPayload(
                                job_batch_id=job_batch_id,
                                miner_hotkey=miner.hotkey,
                                miner_coldkey=miner.coldkey,
                                miner_address=miner.axon_info.ip,
                                miner_port=miner.axon_info.port,
                            ),
                            encrypted_files=encrypted_files,
                        )
                    )
                    for miner in miners
                ]

                for miner, job in zip(miners, jobs):
                    task_info[job] = {
                        "miner_hotkey": miner.hotkey,
                        "miner_address": miner.axon_info.ip,
                        "miner_port": miner.axon_info.port,
                        "job_batch_id": job_batch_id,
                    }

                try:
                    total_gpu_model_count_map = {}
                    all_job_results = {}

                    # Run all jobs with asyncio.wait and set a timeout
                    done, pending = await asyncio.wait(jobs, timeout=JOB_TIME_OUT - 50)

                    # Process completed jobs
                    for task in done:
                        try:
                            result = task.result()
                            if result:
                                miner_hotkey = result.get("miner_hotkey")
                                job_results: list[JobResult] = result.get("results", [])

                                logger.info(
                                    _m(
                                        "[sync] Job_Result",
                                        extra=get_extra_info(
                                            {
                                                **self.default_extra,
                                                "miner_hotkey": miner_hotkey,
                                                "results": len(job_results)
                                            }
                                        ),
                                    ),
                                )

                                all_job_results[miner_hotkey] = job_results

                                for job_result in job_results:
                                    total_gpu_model_count_map[job_result.gpu_model] = total_gpu_model_count_map.get(job_result.gpu_model, 0) + job_result.gpu_count

                            else:
                                info = task_info.get(task, {})
                                miner_hotkey = info.get("miner_hotkey", "unknown")
                                job_batch_id = info.get("job_batch_id", "unknown")
                                logger.error(
                                    _m(
                                        "[sync] No_Job_Result",
                                        extra=get_extra_info(
                                            {
                                                **self.default_extra,
                                                "miner_hotkey": miner_hotkey,
                                                "job_batch_id": job_batch_id,
                                            }
                                        ),
                                    ),
                                )

                        except Exception as e:
                            logger.error(
                                _m(
                                    "[sync] Error processing job result",
                                    extra=get_extra_info(
                                        {
                                            **self.default_extra,
                                            "job_batch_id": job_batch_id,
                                            "error": str(e),
                                        }
                                    ),
                                ),
                            )

                    # Handle pending jobs (those that did not complete within the timeout)
                    if pending:
                        for task in pending:
                            info = task_info.get(task, {})
                            miner_hotkey = info.get("miner_hotkey", "unknown")
                            job_batch_id = info.get("job_batch_id", "unknown")

                            logger.error(
                                _m(
                                    "[sync] Job_Timeout",
                                    extra=get_extra_info(
                                        {
                                            **self.default_extra,
                                            "miner_hotkey": miner_hotkey,
                                            "job_batch_id": job_batch_id,
                                        }
                                    ),
                                ),
                            )
                            task.cancel()

                    open_fd_count = len(os.listdir(f'/proc/self/fd'))

                    logger.info(
                        _m(
                            "Total GPU model count",
                            extra=get_extra_info(
                                {
                                    **self.default_extra,
                                    "job_batch_id": job_batch_id,
                                    "data": total_gpu_model_count_map,
                                }
                            ),
                        ),
                    )

                    for miner_hotkey, results in all_job_results.items():
                        for result in results:
                            score = await self.calc_job_score(total_gpu_model_count_map, result)
                            self.miner_scores[miner_hotkey] = self.miner_scores.get(miner_hotkey, 0) + score

                    logger.info(
                        _m(
                            "[sync] All Jobs finished",
                            extra=get_extra_info(
                                {
                                    **self.default_extra,
                                    "job_batch_id": job_batch_id,
                                    "miner_scores": self.miner_scores,
                                    "open_fd_count": open_fd_count,
                                }
                            ),
                        ),
                    )

                except Exception as e:
                    logger.error(
                        _m(
                            "[sync] Unexpected error",
                            extra=get_extra_info(
                                {
                                    **self.default_extra,
                                    "job_batch_id": job_batch_id,
                                    "error": str(e),
                                }
                            ),
                        ),
                    )
            else:
                remaining_blocks = (
                    current_block // settings.BLOCKS_FOR_JOB + 1
                ) * settings.BLOCKS_FOR_JOB - current_block

                logger.info(
                    _m(
                        "[sync] Remaining blocks for next job",
                        extra=get_extra_info(
                            {
                                **self.default_extra,
                                "remaining_blocks": remaining_blocks,
                                "last_job_run_blocks": self.last_job_run_blocks,
                                "current_block": current_block,
                            }
                        ),
                    ),
                )
        except Exception as e:
            logger.error(
                _m(
                    "[sync] Unknown error",
                    extra=get_extra_info(
                        {
                            **self.default_extra,
                            "error": str(e),
                        }
                    ),
                ),
                exc_info=True,
            )

    async def start(self):
        logger.info(
            _m(
                "[start] Starting Validator in background",
                extra=get_extra_info(self.default_extra),
            ),
        )
        try:
            await self.initiate_services()
            self.should_exit = False

            while not self.should_exit:
                await self.sync()

                # sync every 12 seconds
                await asyncio.sleep(SYNC_CYCLE)

        except KeyboardInterrupt:
            logger.info(
                _m(
                    "[start] Validator killed by keyboard interrupt",
                    extra=get_extra_info(self.default_extra),
                ),
            )
            exit()
        except Exception as e:
            logger.info(
                _m(
                    "[start] Unknown error",
                    extra=get_extra_info({**self.default_extra, "error": str(e)}),
                ),
            )

    async def stop(self):
        logger.info(
            _m(
                "[stop] Stopping Validator process",
                extra=get_extra_info(self.default_extra),
            ),
        )

        try:
            await self.redis_service.set(MINER_SCORES_KEY, json.dumps(self.miner_scores))
        except Exception as e:
            logger.info(
                _m(
                    "[stop] Failed to save miner_scores",
                    extra=get_extra_info({**self.default_extra, "error": str(e)}),
                ),
            )

        self.should_exit = True
