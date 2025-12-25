"""
说明：
1. 先通过账号/密码登录（会弹出验证码图片窗口，需要你输入验证码）。
2. 登录成功后程序自动获取 token 与 batchId（若有多个，默认取第一个），并将其写入运行时配置。
3. 然后开始异步选课流程（双队列：主队列 + 重试队列）。
注意：请确保 Python 环境已安装 requests、aiohttp、cryptography、Pillow 等依赖。
"""

import asyncio
import aiohttp
import time
import heapq
from typing import List, Dict
import logging
from datetime import datetime, timedelta
import sys
from dataclasses import dataclass, field
import random

# 用于同步登录部分
import requests
import base64
import json
import tkinter as tk
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from io import BytesIO
from PIL import Image, ImageTk
import urllib.parse

global_username = ""
global_password = ""

# === 全局配置（登录前会动态写入 AUTH_TOKEN 与 BATCH_ID） ===
CONFIG = {
    # =======================
    # 一、认证 & 会话相关（登录后自动填写）
    # =======================
    "AUTH_TOKEN": "",
    "BATCH_ID": "",

    # =======================
    # 二、目标课程配置（示例，可修改）
    # =======================
    "TARGET_COURSES": [
        {"course_name": "", "teacher": ""},
    ],

    # =======================
    # 三、时间控制相关
    # =======================
    "SELECTION_START_TIME": "2025-12-24 23:12:00",
    "TIME_ADVANCE_SECONDS": 0.1,
    "LIST_FETCH_MINUTES_BEFORE": 5,

    # =======================
    # 四、主任务（正常选课请求）调度
    # =======================
    "REQUEST_INTERVAL": 0.6,
    "MAX_CONCURRENT_REQUESTS": 1,
    "TIMEOUT": 1.2,
    "MIN_REQUEST_INTERVAL": 0.4,
    "JITTER": 0.1,

    # =======================
    # 五、重试任务配置（失败补偿）
    # =======================
    "MAX_RETRIES": 5,
    "RETRY_DELAY": 1.2,
    "MAX_RETRY_CONCURRENT": 1,
    "RETRY_INTERVAL": 2.0,

    # =======================
    # 六、运行状态 & 心跳
    # =======================
    "HEARTBEAT_INTERVAL": 5,

    # =======================
    # 七、课程类型映射
    # =======================
    "COURSE_TYPE_MAP": {
        "通选课": "XGKC",
        "通识选修课程": "XGKC",
        "体育课": "TYKC",
    },
}

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


@dataclass(order=True)
class ScheduledTask:
    execute_time: float
    priority: int = field(compare=True)
    task_id: int = field(compare=False)
    request: 'CourseRequest' = field(compare=False)
    is_retry: bool = field(compare=False, default=False)


@dataclass
class CourseRequest:
    clazz_info: Dict
    task_id: int
    attempts: int = 0
    success: bool = False
    last_attempt: float = 0
    next_attempt_time: float = 0
    retry_scheduled: bool = False


class DualQueueScheduler:
    def __init__(self, config: Dict):
        self.config = config
        self.main_queue = []
        self.retry_queue = []

        self.main_semaphore = asyncio.Semaphore(config["MAX_CONCURRENT_REQUESTS"])
        self.retry_semaphore = asyncio.Semaphore(config["MAX_RETRY_CONCURRENT"])

        self.last_main_request_time = 0
        self.last_retry_request_time = 0
        self.scheduler_running = True

        self.main_processed = 0
        self.retry_processed = 0

    def schedule_main_task(self, request: CourseRequest, delay: float = 0):
        execute_time = time.time() + delay + random.uniform(0, self.config["JITTER"])
        task = ScheduledTask(
            execute_time=execute_time,
            priority=0,
            task_id=request.task_id,
            request=request,
            is_retry=False
        )
        heapq.heappush(self.main_queue, task)
        logger.debug(f"📋 主任务调度: 任务{request.task_id}, {delay:.2f}s后执行")

    def schedule_retry_task(self, request: CourseRequest, delay: float = 0):
        execute_time = time.time() + delay + random.uniform(0, self.config["JITTER"])
        task = ScheduledTask(
            execute_time=execute_time,
            priority=1,
            task_id=request.task_id,
            request=request,
            is_retry=True
        )
        heapq.heappush(self.retry_queue, task)
        request.retry_scheduled = True
        logger.debug(f"🔄 重试任务调度: 任务{request.task_id}, {delay:.2f}s后执行")

    async def process_main_tasks(self, process_func):
        min_interval = self.config["MIN_REQUEST_INTERVAL"]
        while self.scheduler_running:
            current_time = time.time()
            if self.main_queue and self.main_queue[0].execute_time <= current_time:
                task = heapq.heappop(self.main_queue)

                time_since_last = current_time - self.last_main_request_time
                if time_since_last < min_interval:
                    await asyncio.sleep(min_interval - time_since_last)

                async with self.main_semaphore:
                    await process_func(task.request, is_retry=False)
                    self.last_main_request_time = time.time()
                    self.main_processed += 1

            else:
                if self.main_queue:
                    wait_time = max(0.0, self.main_queue[0].execute_time - current_time)
                    await asyncio.sleep(min(wait_time, 0.1))
                else:
                    await asyncio.sleep(0.1)

    async def process_retry_tasks(self, process_func):
        retry_interval = self.config["RETRY_INTERVAL"]
        while self.scheduler_running:
            current_time = time.time()
            if self.retry_queue and self.retry_queue[0].execute_time <= current_time:
                task = heapq.heappop(self.retry_queue)

                time_since_last = current_time - self.last_retry_request_time
                if time_since_last < retry_interval:
                    await asyncio.sleep(retry_interval - time_since_last)

                async with self.retry_semaphore:
                    await process_func(task.request, is_retry=True)
                    self.last_retry_request_time = time.time()
                    self.retry_processed += 1

            else:
                if self.retry_queue:
                    wait_time = max(0.0, self.retry_queue[0].execute_time - current_time)
                    await asyncio.sleep(min(wait_time, 0.1))
                else:
                    await asyncio.sleep(0.1)

    async def process_all_tasks(self, process_func):
        main_task = asyncio.create_task(self.process_main_tasks(process_func))
        retry_task = asyncio.create_task(self.process_retry_tasks(process_func))

        try:
            await asyncio.gather(main_task, retry_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.stop()

    def stop(self):
        self.scheduler_running = False

    def get_stats(self):
        return {
            "main_queue_size": len(self.main_queue),
            "retry_queue_size": len(self.retry_queue),
            "main_processed": self.main_processed,
            "retry_processed": self.retry_processed,
            "main_waiting": self.main_semaphore._value < self.config["MAX_CONCURRENT_REQUESTS"],
            "retry_waiting": self.retry_semaphore._value < self.config["MAX_RETRY_CONCURRENT"],
        }


class AsyncCourseSelector:
    def __init__(self, config: Dict):
        self.config = config
        self.session = None
        self.headers = self._setup_headers()
        self.target_clazzes = []
        self.selection_start_time = None
        self.actual_start_time = None
        self.list_fetch_time = None
        self.running = False

        self.requests: List[CourseRequest] = []
        self.success_count = 0
        self.total_attempts = 0
        self.start_time = 0

        self.scheduler = DualQueueScheduler(config)

    def _setup_headers(self) -> Dict:
        # 注意：AUTH_TOKEN 与 BATCH_ID 在登录后会被写入 CONFIG
        return {
            "Authorization": self.config.get("AUTH_TOKEN", ""),
            "batchid": self.config.get("BATCH_ID", ""),
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0",
            "Referer": f"https://newxk.urp.seu.edu.cn/xsxk/elective/grablessons?batchId={self.config.get('BATCH_ID','')}&token={self.config.get('AUTH_TOKEN','')}",
            "Origin": "https://newxk.urp.seu.edu.cn",
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Sec-CH-UA": '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Connection": "keep-alive",
            "Host": "newxk.urp.seu.edu.cn",
        }

    def get_current_time(self) -> datetime:
        return datetime.now()

    def calculate_times(self):
        try:
            self.selection_start_time = datetime.strptime(
                self.config["SELECTION_START_TIME"],
                "%Y-%m-%d %H:%M:%S"
            )

            self.actual_start_time = self.selection_start_time - timedelta(
                seconds=self.config["TIME_ADVANCE_SECONDS"]
            )

            self.list_fetch_time = self.selection_start_time - timedelta(
                minutes=self.config["LIST_FETCH_MINUTES_BEFORE"]
            )

            logger.info(f"🎯 配置选课时间: {self.selection_start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"⏰ 实际开始时间: {self.actual_start_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        except Exception as e:
            logger.error(f"时间配置错误: {str(e)}")
            sys.exit(1)

    def wait_until_time_sync(self, target_time: datetime):
        logger.info(f"⏳ 等待到 {target_time.strftime('%H:%M:%S.%f')[:-3]}")

        last_print_time = time.time()
        print_interval = 1.0

        while True:
            current_time = self.get_current_time()
            time_diff = (target_time - current_time).total_seconds()

            if time_diff <= 0:
                logger.info(f"✅ 到达目标时间")
                return

            current_time_epoch = time.time()

            if current_time_epoch - last_print_time >= print_interval:
                if time_diff > 60:
                    logger.info(
                        f"   剩余时间: {int(time_diff // 3600):02d}:{int((time_diff % 3600) // 60):02d}:{int(time_diff % 60):02d}")
                elif time_diff > 10:
                    logger.info(f"   剩余时间: {int(time_diff):02d}秒")
                else:
                    logger.info(f"   倒计时: {int(time_diff):02d}秒")
                last_print_time = current_time_epoch

            time.sleep(0.01)

    async def get_course_list_async(self):
        async with aiohttp.ClientSession() as session:
            course_types = ["TJKC", "XGKC", "TYKC"]
            all_clazzes = []

            for course_type in course_types:
                url = f"https://newxk.urp.seu.edu.cn/xsxk/elective/clazz/list?batchId={self.config['BATCH_ID']}"
                payload = {
                    "campus": "1",
                    "teachingClassType": course_type,
                    "pageNumber": 1,
                    "pageSize": 200
                }

                try:
                    async with session.post(url, headers=self.headers, json=payload,
                                            timeout=self.config["TIMEOUT"]) as response:
                        data = await response.json()

                        if data.get("code") == 200 and "data" in data:
                            rows = data["data"].get("rows", [])

                            for item in rows:
                                if "tcList" in item and item["tcList"]:
                                    for clazz in item.get("tcList", []):
                                        clazz_info = self._extract_clazz_info(clazz, course_type, item)
                                        all_clazzes.append(clazz_info)
                                else:
                                    clazz_info = self._extract_clazz_info(item, course_type, item)
                                    all_clazzes.append(clazz_info)

                except Exception as e:
                    logger.error(f"获取 {course_type} 类型课程时发生错误: {str(e)}")

            logger.info(f"✅ 总共获取到 {len(all_clazzes)} 个教学班")
            return all_clazzes

    def _extract_clazz_info(self, clazz_data: Dict, clazz_type: str, parent_course: Dict = None) -> Dict:
        course_name = clazz_data.get("KCM", "") or (parent_course.get("KCM", "") if parent_course else "")
        course_code = clazz_data.get("KCH", "") or (parent_course.get("KCH", "") if parent_course else "")

        sport_name = clazz_data.get("sportName", "")
        if sport_name:
            course_name = sport_name

        teacher = clazz_data.get("SKJS", "") or clazz_data.get("SKJSZC", "")

        return {
            "clazzId": clazz_data.get("JXBID", ""),
            "secretVal": clazz_data.get("secretVal", ""),
            "teacher": teacher,
            "course_name": course_name,
            "course_type": clazz_data.get("KCXZ", "") or (parent_course.get("KCXZ", "") if parent_course else ""),
            "course_category": clazz_data.get("KCLB", "") or (parent_course.get("KCLB", "") if parent_course else ""),
            "clazz_type": clazz_type,
            "sport_name": sport_name
        }

    def determine_clazz_type(self, clazz_info: Dict) -> str:
        if "clazz_type" in clazz_info and clazz_info["clazz_type"]:
            return clazz_info["clazz_type"]

        course_category = clazz_info.get("course_category", "")
        course_type = clazz_info.get("course_type", "")
        course_name = clazz_info.get("course_name", "")

        if ("体育" in course_name or "体育" in course_category or "军体类" in course_category or
                clazz_info.get("sport_name") or "《标准》锻炼课" in course_name):
            return "TYKC"

        for category_key, clazz_type in self.config["COURSE_TYPE_MAP"].items():
            if (category_key in course_category or category_key in course_type or category_key in course_name):
                return clazz_type

        return "TJKC"

    def find_target_courses(self, all_clazzes: List[Dict]) -> List[Dict]:
        target_clazzes = []

        for target in self.config["TARGET_COURSES"]:
            target_name = target["course_name"]
            target_teacher = target["teacher"]

            found = False
            for clazz in all_clazzes:
                course_name = clazz["course_name"]
                teacher = clazz["teacher"]
                sport_name = clazz.get("sport_name", "")

                name_match = (target_name == course_name or
                              (sport_name and target_name == sport_name))

                teacher_match = (not target_teacher or teacher == target_teacher)

                if name_match and teacher_match:
                    clazz_type = self.determine_clazz_type(clazz)
                    clazz["determined_clazz_type"] = clazz_type
                    target_clazzes.append(clazz)
                    logger.info(f"✅ 找到: {course_name} - {teacher} (类型: {clazz_type})")
                    found = True
                    break

            if not found:
                logger.warning(f"未找到: {target_name} - {target_teacher}")

        return target_clazzes

    async def execute_course_selection(self, request: CourseRequest, is_retry: bool = False) -> bool:
        request.attempts += 1
        request.last_attempt = time.time()
        self.total_attempts += 1

        clazz_info = request.clazz_info
        clazz_type = clazz_info.get("determined_clazz_type", "TJKC")

        url = "https://newxk.urp.seu.edu.cn/xsxk/elective/clazz/add"
        data = {
            "clazzType": clazz_type,
            "clazzId": clazz_info["clazzId"],
            "secretVal": clazz_info["secretVal"]
        }

        headers = self.headers.copy()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        start_time = time.time()
        course_key = f"{clazz_info['course_name']} - {clazz_info['teacher']}"
        prefix = "[重试]" if is_retry else "[主]"

        try:
            async with self.session.post(url, headers=headers, data=data,
                                         timeout=self.config["TIMEOUT"]) as response:
                result = await response.json()
                elapsed_time = time.time() - start_time

                if result.get("code") == 200:
                    logger.info(f"{prefix}任务{request.task_id} ✅ 成功! {course_key} (耗时: {elapsed_time:.3f}s)")
                    return True
                else:
                    error_msg = result.get("msg", "未知错误")
                    logger.warning(
                        f"{prefix}任务{request.task_id} ❌ 失败: {course_key} - {error_msg} (耗时: {elapsed_time:.3f}s)")
                    return await self.handle_selection_error(request, error_msg, course_key, is_retry)

        except asyncio.TimeoutError:
            logger.error(f"{prefix}任务{request.task_id} ⏱️  超时: {course_key}")
            return await self.handle_selection_error(request, "请求超时", course_key, is_retry)
        except Exception as e:
            logger.error(f"{prefix}任务{request.task_id} 🔧 异常: {course_key} - {str(e)}")
            return await self.handle_selection_error(request, f"异常: {str(e)}", course_key, is_retry)

    async def handle_selection_error(self, request: CourseRequest, error_msg: str,
                                     course_key: str, is_retry: bool) -> bool:
        base_delay = self.config["RETRY_DELAY"]
        prefix = "[重试]" if is_retry else "[主]"

        if "满" in error_msg or "满员" in error_msg or "名额已满" in error_msg:
            if request.attempts >= self.config["MAX_RETRIES"]:
                logger.warning(f"{prefix}任务{request.task_id} ⛔ 课程已满且达到最大重试次数，放弃")
                return False

            delay = min(10.0, base_delay * 3.0)
            logger.info(f"{prefix}任务{request.task_id} ⛔ 课程已满，{delay:.1f}s后重试")
            self.scheduler.schedule_retry_task(request, delay)
            return False

        elif "请求过快" in error_msg or "频率" in error_msg or "过快" in error_msg:
            if request.attempts >= self.config["MAX_RETRIES"]:
                logger.warning(f"{prefix}任务{request.task_id} ⚠️  请求过快且达到最大重试次数，放弃")
                return False

            backoff_time = min(2.0, base_delay * (2 ** min(request.attempts, 4)))
            logger.info(f"{prefix}任务{request.task_id} ⏸️  请求过快，{backoff_time:.1f}s后重试")
            self.scheduler.schedule_retry_task(request, backoff_time)
            return False

        elif "选课暂未开始" in error_msg or "暂未开始" in error_msg:
            if request.attempts >= self.config["MAX_RETRIES"] * 2:
                logger.warning(f"{prefix}任务{request.task_id} ⚠️  时间未到且达到最大重试次数，放弃")
                return False

            delay = 0.5
            logger.info(f"{prefix}任务{request.task_id} ⏰ 时间未到，{delay:.1f}s后重试")
            self.scheduler.schedule_retry_task(request, delay)
            return False

        elif "该课程已在选课结果中" in error_msg:
            logger.info(f"{prefix}任务{request.task_id} ✅ 已选上该课程")
            request.success = True
            self.success_count += 1
            return True

        elif "身份验证失败" in error_msg or "登录" in error_msg or "token" in error_msg:
            logger.error(f"{prefix}任务{request.task_id} ❌ Token已过期，请重新获取")
            self.running = False
            return False

        else:
            if request.attempts >= self.config["MAX_RETRIES"]:
                logger.warning(f"{prefix}任务{request.task_id} ⚠️  其他错误且达到最大重试次数，放弃")
                return False

            logger.info(f"{prefix}任务{request.task_id} 🔄 其他错误，{base_delay:.1f}s后重试")
            self.scheduler.schedule_retry_task(request, base_delay)
            return False

    async def process_request_with_scheduler(self, request: CourseRequest, is_retry: bool = False):
        if not self.running or request.success:
            return

        if request.attempts >= self.config["MAX_RETRIES"]:
            logger.warning(f"[任务{request.task_id}] ⚠️  达到最大重试次数，停止尝试")
            return

        success = await self.execute_course_selection(request, is_retry)

        if success:
            request.success = True
            self.success_count += 1
            logger.info(f"[任务{request.task_id}] 🎉 选课成功！")

    async def start_selection_async(self):
        logger.info("🚀 开始异步选课")
        logger.info(f"📊 启动 {len(self.target_clazzes)} 个主任务")
        logger.info(f"⏱️  主请求间隔: {self.config['MIN_REQUEST_INTERVAL'] * 1000:.0f}ms")
        logger.info(f"🔀 主请求并发: {self.config['MAX_CONCURRENT_REQUESTS']}")
        logger.info(f"🔄 重试请求间隔: {self.config['RETRY_INTERVAL'] * 1000:.0f}ms")
        logger.info(f"⚡ 重试并发: {self.config['MAX_RETRY_CONCURRENT']}")

        # 创建 aiohttp 会话并保持到程序结束
        self.session = aiohttp.ClientSession()

        current_time = time.time()
        for i, request in enumerate(self.requests):
            delay = i * self.config["REQUEST_INTERVAL"]
            self.scheduler.schedule_main_task(request, delay)
            logger.debug(f"调度主任务{i}: {delay:.1f}s后执行")

        self.start_time = current_time

        async def heartbeat():
            heartbeat_count = 0
            while self.running:
                await asyncio.sleep(self.config["HEARTBEAT_INTERVAL"])

                stats = self.scheduler.get_stats()
                heartbeat_count += 1

                if heartbeat_count % 5 == 0:
                    logger.info("📈 详细调度状态:")
                    logger.info(f"   主队列: {stats['main_queue_size']} 个任务")
                    logger.info(f"   重试队列: {stats['retry_queue_size']} 个任务")
                    logger.info(f"   主任务完成: {stats['main_processed']} 个")
                    logger.info(f"   重试任务完成: {stats['retry_processed']} 个")
                    logger.info(f"   主任务等待: {'是' if stats['main_waiting'] else '否'}")
                    logger.info(f"   重试任务等待: {'是' if stats['retry_waiting'] else '否'}")

                running_count = sum(1 for r in self.requests if not r.success)
                logger.info(
                    f"📊 状态: 成功 {self.success_count}/{len(self.requests)}，"
                    f"进行中 {running_count}，总尝试 {self.total_attempts}"
                )

        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            await self.scheduler.process_all_tasks(self.process_request_with_scheduler)

            if all(r.success for r in self.requests):
                logger.info("✅ 所有任务已完成")
            else:
                while (self.scheduler.main_queue or self.scheduler.retry_queue):
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info("调度被取消")
        finally:
            self.running = False
            self.scheduler.stop()
            heartbeat_task.cancel()

            try:
                await self.session.close()
            except:
                pass

            end_time = time.time()
            total_duration = end_time - self.start_time
            logger.info("=" * 60)
            logger.info("📊 异步选课统计汇总:")
            logger.info(f"⏱️  总耗时: {total_duration:.3f}秒")
            logger.info(f"🔢 总请求次数: {self.total_attempts}")
            logger.info(f"✅ 成功课程: {self.success_count}/{len(self.target_clazzes)}")

            stats = self.scheduler.get_stats()
            logger.info(f"📋 主任务处理: {stats['main_processed']} 个")
            logger.info(f"🔄 重试任务处理: {stats['retry_processed']} 个")

            for request in self.requests:
                status = "成功" if request.success else f"失败({request.attempts}次尝试)"
                retry_info = f", 重试调度:{'是' if request.retry_scheduled else '否'}"
                logger.info(f"  任务{request.task_id}: {request.clazz_info['course_name']} - {status}{retry_info}")

            logger.info("=" * 60)

    async def run(self):
        logger.info("🎓 东南大学选课助手 (双队列调度版)")
        logger.info("=" * 60)

        current_time = self.get_current_time()
        logger.info(f"⏰ 启动时间: {current_time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")

        self.calculate_times()

        if current_time < self.list_fetch_time:
            self.wait_until_time_sync(self.list_fetch_time)

        logger.info("📋 异步获取课程列表...")
        all_clazzes = await self.get_course_list_async()

        if not all_clazzes:
            logger.error("❌ 无法获取课程列表")
            return

        self.target_clazzes = self.find_target_courses(all_clazzes)

        if not self.target_clazzes:
            logger.error("❌ 未找到目标课程")
            return

        logger.info(f"🎯 找到 {len(self.target_clazzes)} 个目标教学班")

        for i, clazz_info in enumerate(self.target_clazzes):
            request = CourseRequest(clazz_info=clazz_info, task_id=i)
            self.requests.append(request)

        current_time = self.get_current_time()
        if current_time < self.actual_start_time:
            self.wait_until_time_sync(self.actual_start_time)

        logger.info("🎬 选课时间到，开始双队列调度抢课！")
        self.running = True
        await self.start_selection_async()


# =========================
# 同步登录模块（原 SEULogin 类，略作小修改以便在合并文件中使用）
# =========================
class SEULogin:
    def __init__(self):
        self.session = requests.Session()
        self.base_url = "https://newxk.urp.seu.edu.cn"

        # 这里的固定 Cookie 可按需修改或清空
        self.session.cookies.update({
            '_ga': 'GA1.1.54247787.1740240195',
            '_ga_4CSM3ZYBN3': 'GS1.1.1740240195.1.1.1740240240.0.0.0',
            'route': '1da7c85a7f1b936f2b579ffd66f4ba16'
        })

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6',
            'Connection': 'keep-alive',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Host': 'newxk.urp.seu.edu.cn',
            'Origin': self.base_url,
            'Referer': f'{self.base_url}/xsxk/profile/index.html',
            'Sec-Ch-Ua': '"Microsoft Edge";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'X-Requested-With': 'XMLHttpRequest',
        }

        self.uuid = None
        self.token = None
        self.batch_ids = []

    def init_session(self):
        try:
            index_url = f"{self.base_url}/xsxk/profile/index.html"
            logger.info("正在访问首页获取会话...")
            response = self.session.get(index_url, headers=self.headers, timeout=10)
            logger.info(f"首页访问状态码: {response.status_code}")
            logger.debug(f"当前会话Cookie: {self.session.cookies.get_dict()}")

            return response.status_code == 200

        except Exception as e:
            logger.error(f"初始化会话时出错: {e}")
            return False

    def encrypt_password(self, password):
        key = "MWMqg2tPcDkxcm11"
        key_bytes = key.encode('utf-8')
        plaintext = password.encode('utf-8')
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(plaintext) + padder.finalize()
        cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        return base64.b64encode(ciphertext).decode('utf-8')

    def get_captcha(self):
        captcha_url = f"{self.base_url}/xsxk/auth/captcha"
        captcha_headers = self.headers.copy()
        captcha_headers['Content-Length'] = '0'
        if 'Content-Type' in captcha_headers:
            del captcha_headers['Content-Type']
        try:
            logger.info("发送验证码请求...")
            response = self.session.post(captcha_url, headers=captcha_headers, timeout=10)
            logger.debug(f"验证码响应状态码: {response.status_code}")
            try:
                result = response.json()
                if result.get('code') == 200:
                    data = result.get('data', {})
                    captcha_base64 = data.get('captcha', '')
                    self.uuid = data.get('uuid', '')
                    if 'base64,' in captcha_base64:
                        captcha_base64 = captcha_base64.split('base64,')[1]
                    image_data = base64.b64decode(captcha_base64)
                    logger.info(f"成功获取验证码UUID: {self.uuid}")
                    return image_data, self.uuid
                else:
                    logger.error(f"获取验证码失败: {result.get('msg')}")
                    return None, None
            except json.JSONDecodeError:
                logger.error("验证码响应不是JSON格式")
                return None, None

        except Exception as e:
            logger.exception("获取验证码时出错")
            return None, None

    def show_captcha_dialog(self, image_data):
        root = tk.Tk()
        root.title("验证码输入")
        # 简化窗口大小自适应
        try:
            image = Image.open(BytesIO(image_data))
            image = image.resize((150, 80), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception as e:
            logger.error(f"加载验证码图片失败: {e}")
            root.destroy()
            return None

        captcha_var = tk.StringVar()

        tk.Label(root, image=photo).pack(pady=8)
        tk.Label(root, text="请输入验证码:").pack()
        entry = tk.Entry(root, textvariable=captcha_var, font=('Arial', 14), justify='center')
        entry.pack(pady=6)
        entry.focus_set()

        def on_confirm():
            root.quit()

        tk.Button(root, text="确定", command=on_confirm, width=10).pack(pady=6)
        root.bind('<Return>', lambda event: on_confirm())

        root.mainloop()
        value = captcha_var.get().strip()
        root.destroy()
        return value if value else None

    def login(self, username, password, captcha_value):
        login_url = f"{self.base_url}/xsxk/auth/login"
        encrypted_password = self.encrypt_password(password)
        form_data = {
            'loginname': username,
            'password': encrypted_password,
            'captcha': captcha_value,
            'uuid': self.uuid
        }
        encoded_data = urllib.parse.urlencode(form_data)
        login_headers = self.headers.copy()
        login_headers['Content-Length'] = str(len(encoded_data))
        try:
            logger.info("发送登录请求...")
            response = self.session.post(
                login_url,
                headers=login_headers,
                data=encoded_data,
                timeout=10
            )
            logger.info(f"登录响应状态码: {response.status_code}")
            logger.debug(f"响应头: {dict(response.headers)}")
            logger.debug(f"当前Cookie: {self.session.cookies.get_dict()}")

            if response.status_code == 200:
                try:
                    result = response.json()
                    logger.debug(f"登录返回 JSON: {result}")
                    if result.get('code') == 200:
                        self.token = result.get('data', {}).get('token', '')
                        elective_batch_list = result.get('data', {}).get('student', {}).get('electiveBatchList', [])
                        self.batch_ids = [batch.get('code') for batch in elective_batch_list if batch.get('code')]
                        logger.info("登录成功，提取到 token 与 batchIds")
                        return True, result
                    else:
                        logger.error(f"登录失败: {result.get('msg')}")
                        return False, result
                except json.JSONDecodeError:
                    logger.error("登录响应无法解析为 JSON")
                    return False, {"msg": "服务器返回非JSON"}
            else:
                logger.error(f"HTTP错误: {response.status_code}")
                return False, {"msg": f"HTTP {response.status_code}"}
        except requests.exceptions.Timeout:
            logger.error("登录请求超时")
            return False, {"msg": "请求超时"}
        except Exception as e:
            logger.exception("登录时出错")
            return False, None

    def manual_login(self, username, password):
        if not self.init_session():
            logger.error("会话初始化失败")
            return False, None

        image_data, uuid = self.get_captcha()
        if not image_data or not uuid:
            logger.error("获取验证码失败")
            return False, None

        captcha_value = self.show_captcha_dialog(image_data)
        if not captcha_value:
            logger.info("用户取消验证码输入")
            return False, None

        success, result = self.login(username, password, captcha_value)
        return success, result

    def get_login_result(self):
        return {
            'token': self.token,
            'batch_ids': self.batch_ids,
            'uuid': self.uuid
        }


# =========================
# 主流程：先登录 -> 更新 CONFIG -> 启动异步选课
# =========================
async def run_selector_after_login(config: Dict):
    # 登录
    logger.info("=" * 60)
    logger.info("请先输入账号与密码（密码输入将隐藏）")
    username = global_username;
    password = global_password;

    login_client = SEULogin()
    success, _ = login_client.manual_login(username, password)

    if not success:
        logger.error("登录失败，退出程序")
        return

    login_result = login_client.get_login_result()
    token = login_result.get('token', '')
    batch_ids = login_result.get('batch_ids', [])

    if not token:
        logger.error("未获取到 token，退出")
        return

    if not batch_ids:
        logger.warning("未获取到 batchId 列表，将继续但部分接口可能失败（请确认账号是否有选课批次）")

    # 写入 CONFIG（选第一个 batchId）
    config['AUTH_TOKEN'] = token
    if batch_ids:
        config['BATCH_ID'] = batch_ids[0]
    else:
        config['BATCH_ID'] = config.get('BATCH_ID', '')

    logger.info(f"已写入 CONFIG['AUTH_TOKEN'] 与 CONFIG['BATCH_ID']（batch使用: {config['BATCH_ID']})")

    # 创建并运行异步选课器
    selector = AsyncCourseSelector(config)
    try:
        await selector.run()
    except KeyboardInterrupt:
        logger.info("用户中断")
        selector.running = False
    except Exception as e:
        logger.exception(f"选课过程中出错: {e}")
        selector.running = False


def main():
    logger.info("📝 合并版：登录 + 异步选课（双队列）")
    logger.info("=" * 60)
    logger.info(f"目标课程数（配置）: {len(CONFIG['TARGET_COURSES'])}")
    try:
        asyncio.run(run_selector_after_login(CONFIG))
    except KeyboardInterrupt:
        logger.info("\n👋 程序已退出")


if __name__ == "__main__":
    main()
