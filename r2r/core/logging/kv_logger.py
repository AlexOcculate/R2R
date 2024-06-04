import json
import logging
import os
import re
import numpy as np
import uuid
from abc import abstractmethod
from datetime import datetime
from typing import Optional

import asyncpg
from pydantic import BaseModel

from ..providers.base_provider import Provider, ProviderConfig

logger = logging.getLogger(__name__)


class RunInfo(BaseModel):
    run_id: uuid.UUID
    log_type: str


class LoggingConfig(ProviderConfig):
    provider: str = "local"
    log_table: str = "logs"
    log_info_table: str = "logs_pipeline_info"
    log_throughput_table: str = "throughput_logs"
    logging_path: Optional[str] = None

    def validate(self) -> None:
        pass

    @property
    def supported_providers(self) -> list[str]:
        return ["local", "postgres", "redis"]


class KVLoggingProvider(Provider):
    @abstractmethod
    async def close(self):
        pass

    @abstractmethod
    async def log(self, log_id: uuid.UUID, key: str, value: str):
        pass

    @abstractmethod
    async def get_run_info(
        self,
        limit: int = 10,
        log_type_filter: Optional[str] = None,
    ) -> list[RunInfo]:
        pass

    @abstractmethod
    async def get_logs(
        self, run_ids: list[uuid.UUID], limit_per_run: int
    ) -> list:
        pass


class LocalKVLoggingProvider(KVLoggingProvider):
    def __init__(self, config: LoggingConfig):
        self.log_table = config.log_table
        self.log_info_table = config.log_info_table
        self.log_throughput_table = config.log_throughput_table
        self.logging_path = config.logging_path or os.getenv(
            "LOCAL_DB_PATH", "local.sqlite"
        )
        if not self.logging_path:
            raise ValueError(
                "Please set the environment variable LOCAL_DB_PATH."
            )
        self.conn = None
        try:
            import aiosqlite

            self.aiosqlite = aiosqlite
        except ImportError:
            raise ImportError(
                "Please install aiosqlite to use the LocalKVLoggingProvider."
            )

    async def init(self):
        self.conn = await self.aiosqlite.connect(self.logging_path)
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.log_table} (
                timestamp DATETIME,
                log_id TEXT,
                key TEXT,
                value TEXT
            )
            """
        )
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.log_info_table} (
                timestamp DATETIME,
                log_id TEXT UNIQUE,
                log_type TEXT
            )
        """
        )
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.log_throughput_table} (
                timestamp DATETIME,
                num_requests INTEGER,
                request_type TEXT
            )
            """
        )
        await self.conn.commit()

    async def __aenter__(self):
        if self.conn is None:
            await self.init()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def log(
        self,
        log_id: uuid.UUID,
        key: str,
        value: str,
        is_info_log=False,
    ):
        collection = self.log_info_table if is_info_log else self.log_table

        if is_info_log:
            if "type" not in key:
                raise ValueError("Info log keys must contain the text 'type'")
            await self.conn.execute(
                f"INSERT INTO {collection} (timestamp, log_id, log_type) VALUES (datetime('now'), ?, ?)",
                (str(log_id), value),
            )
        else:
            await self.conn.execute(
                f"INSERT INTO {collection} (timestamp, log_id, key, value) VALUES (datetime('now'), ?, ?, ?)",
                (str(log_id), key, value),
            )
        await self.conn.commit()

    async def get_run_info(
        self, limit: int = 10, log_type_filter: Optional[str] = None
    ) -> list[RunInfo]:
        cursor = await self.conn.cursor()
        query = f"SELECT log_id, log_type FROM {self.log_info_table}"
        conditions = []
        params = []
        if log_type_filter:
            conditions.append("log_type = ?")
            params.append(log_type_filter)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        await cursor.execute(query, params)
        rows = await cursor.fetchall()
        return [
            RunInfo(run_id=uuid.UUID(row[0]), log_type=row[1]) for row in rows
        ]
    
    async def log_throughput(self, timestamp: float, num_requests: int, request_type: str):
        await self.conn.execute(
            f"INSERT INTO throughput_logs (timestamp, num_requests, request_type) VALUES (datetime(?, 'unixepoch'), ?, ?)",
            timestamp,
            num_requests,
            request_type,
        )
        await self.conn.commit()

    async def get_throughput_data(self, start_time: Optional[float] = None, end_time: Optional[float] = None):
        cursor = await self.conn.cursor()
        query = "SELECT timestamp, num_requests, request_type FROM throughput_logs"
        params = []
        if start_time and end_time:
            query += " WHERE timestamp BETWEEN datetime(?, 'unixepoch') AND datetime(?, 'unixepoch')"
            params.extend([start_time, end_time])
        await cursor.execute(query, params)
        rows = await cursor.fetchall()
        return [{"timestamp": row[0], "num_requests": row[1], "request_type": row[2]} for row in rows]


    async def get_logs(
        self, run_ids: list[uuid.UUID], limit_per_run: int = 10
    ) -> list:
        if not run_ids:
            raise ValueError("No run ids provided.")
        cursor = await self.conn.cursor()
        placeholders = ",".join(["?" for _ in run_ids])
        query = f"""
        SELECT *
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY log_id ORDER BY timestamp DESC) as rn
            FROM {self.log_table}
            WHERE log_id IN ({placeholders})
        )
        WHERE rn <= ?
        ORDER BY timestamp DESC
        """
        params = [str(ele) for ele in run_ids] + [limit_per_run]
        await cursor.execute(query, params)
        rows = await cursor.fetchall()
        new_rows = []
        for row in rows:
            new_rows.append(
                (row[0], uuid.UUID(row[1]), row[2], row[3], row[4])
            )
        return [
            {desc[0]: row[i] for i, desc in enumerate(cursor.description)}
            for row in new_rows
        ]


class PostgresLoggingConfig(LoggingConfig):
    provider: str = "postgres"
    log_table: str = "logs"
    log_info_table: str = "logs_pipeline_info"
    log_throughput_table: str = "throughput_logs"

    def validate(self) -> None:
        required_env_vars = [
            "POSTGRES_DBNAME",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
        ]
        for var in required_env_vars:
            if not os.getenv(var):
                raise ValueError(f"Environment variable {var} is not set.")

    @property
    def supported_providers(self) -> list[str]:
        return ["postgres"]


class PostgresKVLoggingProvider(KVLoggingProvider):
    def __init__(self, config: PostgresLoggingConfig):
        self.log_table = config.log_table
        self.log_info_table = config.log_info_table
        self.log_throughput_table = config.log_throughput_table
        self.config = config
        self.conn = None
        if not os.getenv("POSTGRES_DBNAME"):
            raise ValueError(
                "Please set the environment variable POSTGRES_DBNAME."
            )
        if not os.getenv("POSTGRES_USER"):
            raise ValueError(
                "Please set the environment variable POSTGRES_USER."
            )
        if not os.getenv("POSTGRES_PASSWORD"):
            raise ValueError(
                "Please set the environment variable POSTGRES_PASSWORD."
            )
        if not os.getenv("POSTGRES_HOST"):
            raise ValueError(
                "Please set the environment variable POSTGRES_HOST."
            )
        if not os.getenv("POSTGRES_PORT"):
            raise ValueError(
                "Please set the environment variable POSTGRES_PORT."
            )

    async def init(self):
        self.conn = await asyncpg.connect(
            database=os.getenv("POSTGRES_DBNAME"),
            user=os.getenv("POSTGRES_USER"),
            password=os.getenv("POSTGRES_PASSWORD"),
            host=os.getenv("POSTGRES_HOST"),
            port=os.getenv("POSTGRES_PORT"),
        )
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.log_table} (
                timestamp TIMESTAMPTZ,
                log_id UUID,
                key TEXT,
                value TEXT
            )
            """
        )
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.log_info_table} (
                timestamp TIMESTAMPTZ,
                log_id UUID UNIQUE,
                log_type TEXT
            )
        """
        )
        await self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.log_throughput_table} (
                timestamp TIMESTAMPTZ,
                num_requests INTEGER,
                request_type TEXT
            )
            """
        )

    async def __aenter__(self):
        if self.conn is None:
            await self.init()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def log(
        self,
        log_id: uuid.UUID,
        key: str,
        value: str,
        is_info_log=False,
    ):
        collection = self.log_info_table if is_info_log else self.log_table

        if is_info_log:
            if "type" not in key:
                raise ValueError(
                    "Info log key must contain the string `type`."
                )
            await self.conn.execute(
                f"INSERT INTO {collection} (timestamp, log_id, log_type) VALUES (NOW(), $1, $2)",
                log_id,
                value,
            )
        else:
            await self.conn.execute(
                f"INSERT INTO {collection} (timestamp, log_id, key, value) VALUES (NOW(), $1, $2, $3)",
                log_id,
                key,
                value,
            )

    async def get_run_info(
        self, limit: int = 10, log_type_filter: Optional[str] = None
    ) -> list[RunInfo]:
        query = f"SELECT log_id, log_type FROM {self.log_info_table}"
        conditions = []
        params = []
        if log_type_filter:
            conditions.append("log_type = $1")
            params.append(log_type_filter)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY timestamp DESC LIMIT $2"
        params.append(limit)
        rows = await self.conn.fetch(query, *params)
        return [
            RunInfo(run_id=row["log_id"], log_type=row["log_type"])
            for row in rows
        ]

    async def get_logs(
        self, run_ids: list[uuid.UUID], limit_per_run: int = 10
    ) -> list:
        if not run_ids:
            raise ValueError("No run ids provided.")

        placeholders = ",".join([f"${i+1}" for i in range(len(run_ids))])
        query = f"""
        SELECT * FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY log_id ORDER BY timestamp DESC) as rn
            FROM {self.log_table}
            WHERE log_id::text IN ({placeholders})
        ) sub
        WHERE sub.rn <= ${len(run_ids) + 1}
        ORDER BY sub.timestamp DESC
        """
        params = [str(run_id) for run_id in run_ids] + [limit_per_run]
        rows = await self.conn.fetch(query, *params)
        return [{key: row[key] for key in row.keys()} for row in rows]
    
    async def log_throughput(self, timestamp: float, num_requests: int, request_type: str):
        async with self.connection.acquire() as conn:
            await conn.execute(
                "INSERT INTO throughput_logs (timestamp, num_requests, request_type) VALUES ($1, $2, $3)",
                timestamp,
                num_requests,
                request_type,
            )

    async def get_throughput_data(self, start_time: Optional[float] = None, end_time: Optional[float] = None):
        async with self.connection.acquire() as conn:
            if start_time and end_time:
                result = await conn.fetch(
                    "SELECT timestamp, num_requests, request_type FROM throughput_logs WHERE timestamp BETWEEN $1 AND $2",
                    start_time,
                    end_time,
                )
            else:
                result = await conn.fetch("SELECT timestamp, num_requests, request_type FROM throughput_logs")
            return [dict(row) for row in result]


class RedisLoggingConfig(LoggingConfig):
    provider: str = "redis"
    log_table: str = "logs"
    log_info_table: str = "logs_pipeline_info"
    throughput_logs_table: str = "throughput_logs"

    def validate(self) -> None:
        required_env_vars = ["REDIS_CLUSTER_IP", "REDIS_CLUSTER_PORT"]
        for var in required_env_vars:
            if not os.getenv(var):
                raise ValueError(f"Environment variable {var} is not set.")

    @property
    def supported_providers(self) -> list[str]:
        return ["redis"]


class RedisKVLoggingProvider(KVLoggingProvider):
    def __init__(self, config: RedisLoggingConfig):
        if not all(
            [
                os.getenv("REDIS_CLUSTER_IP"),
                os.getenv("REDIS_CLUSTER_PORT"),
            ]
        ):
            raise ValueError(
                "Please set the environment variables REDIS_CLUSTER_IP and REDIS_CLUSTER_PORT to run `LoggingDatabaseConnection` with `redis`."
            )
        try:
            from redis.asyncio import Redis
        except ImportError:
            raise ValueError(
                "Error, `redis` is not installed. Please install it using `pip install redis`."
            )

        cluster_ip = os.getenv("REDIS_CLUSTER_IP")
        port = os.getenv("REDIS_CLUSTER_PORT")
        self.redis = Redis(host=cluster_ip, port=port, decode_responses=True)
        self.log_key = config.log_table
        self.log_info_key = config.log_info_table
        self.throughput_logs_key = config.throughput_logs_table

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        await self.redis.close()

    async def log(
        self,
        log_id: uuid.UUID,
        key: str,
        value: str,
        is_info_log=False,
    ):
        timestamp = datetime.now().timestamp()
        log_entry = {
            "timestamp": timestamp,
            "log_id": str(log_id),
            "key": key,
            "value": value,
        }
        if is_info_log:
            if "type" not in key:
                raise ValueError("Metadata keys must contain the text 'type'")
            log_entry["log_type"] = value
            await self.redis.hset(
                self.log_info_key, str(log_id), json.dumps(log_entry)
            )
            await self.redis.zadd(
                f"{self.log_info_key}_sorted", {str(log_id): timestamp}
            )
        else:
            await self.redis.lpush(
                f"{self.log_key}:{str(log_id)}", json.dumps(log_entry)
            )

    async def get_run_info(
        self, limit: int = 10, log_type_filter: Optional[str] = None
    ) -> list[RunInfo]:
        run_info_list = []
        start = 0
        count_per_batch = 100  # Adjust batch size as needed

        while len(run_info_list) < limit:
            log_ids = await self.redis.zrevrange(
                f"{self.log_info_key}_sorted",
                start,
                start + count_per_batch - 1,
            )
            if not log_ids:
                break  # No more log IDs to process

            start += count_per_batch

            for log_id in log_ids:
                log_entry = json.loads(
                    await self.redis.hget(self.log_info_key, log_id)
                )
                if log_type_filter:
                    if log_entry["log_type"] == log_type_filter:
                        run_info_list.append(
                            RunInfo(
                                run_id=uuid.UUID(log_entry["log_id"]),
                                log_type=log_entry["log_type"],
                            )
                        )
                else:
                    run_info_list.append(
                        RunInfo(
                            run_id=uuid.UUID(log_entry["log_id"]),
                            log_type=log_entry["log_type"],
                        )
                    )

                if len(run_info_list) >= limit:
                    break

        return run_info_list[:limit]

    async def get_logs(
        self, run_ids: list[uuid.UUID], limit_per_run: int = 10
    ) -> list:
        logs = []
        for run_id in run_ids:
            raw_logs = await self.redis.lrange(
                f"{self.log_key}:{str(run_id)}", 0, limit_per_run - 1
            )
            for raw_log in raw_logs:
                json_log = json.loads(raw_log)
                json_log["log_id"] = uuid.UUID(json_log["log_id"])
                logs.append(json_log)
        return logs


class KVLoggingSingleton:
    _instance = None
    _is_configured = False

    SUPPORTED_PROVIDERS = {
        "local": LocalKVLoggingProvider,
        "postgres": PostgresKVLoggingProvider,
        "redis": RedisKVLoggingProvider,
    }

    @classmethod
    def get_instance(cls):
        return cls.SUPPORTED_PROVIDERS[cls._config.provider](cls._config)

    @classmethod
    def configure(
        cls, logging_config: Optional[LoggingConfig] = LoggingConfig()
    ):
        if not cls._is_configured:
            cls._config = logging_config
            cls._is_configured = True
        else:
            raise Exception("KVLoggingSingleton is already configured.")

    @classmethod
    async def log(
        cls,
        log_id: uuid.UUID,
        key: str,
        value: str,
        is_info_log=False,
    ):
        try:
            async with cls.get_instance() as provider:
                await provider.log(log_id, key, value, is_info_log=is_info_log)
        except Exception as e:
            logger.error(f"Error logging data: {e}")

    @classmethod
    async def get_run_info(
        cls, limit: int = 10, log_type_filter: Optional[str] = None
    ) -> list[RunInfo]:
        async with cls.get_instance() as provider:
            return await provider.get_run_info(
                limit, log_type_filter=log_type_filter
            )

    @classmethod
    async def get_logs(
        cls, run_ids: list[uuid.UUID], limit_per_run: int = 10
    ) -> list:
        async with cls.get_instance() as provider:
            return await provider.get_logs(run_ids, limit_per_run)
        
    @classmethod
    async def log_throughput(cls, timestamp: float, num_requests: int, request_type: str):
        try:
            async with cls.get_instance() as provider:
                await provider.log_throughput(timestamp, num_requests, request_type)
        except Exception as e:
            logger.error(f"Error logging throughput data: {e}")
    
    @classmethod
    async def get_throughput_data(cls, start_time: Optional[float] = None, end_time: Optional[float] = None):
        async with cls.get_instance() as provider:
            return await provider.get_throughput_data(start_time, end_time)

    @classmethod
    async def get_analytics(cls, pipeline_type: Optional[str] = None):
        #FIXME: add run ideas here to harmonize with the rest of the code
        run_info = await cls.get_run_info(pipeline_type=pipeline_type)
        run_ids = [info.run_id for info in run_info]

        if not run_ids:
            return {
                "error_rates": {},
                "error_distribution": {},
                "retrieval_scores": [],
                "throughput_data": [],
            }

        logs = await cls.get_logs(run_ids=run_ids, limit_per_run=100)

        throughput_data = await cls.get_throughput_data()

        # Process logs to calculate metrics
        processed_logs = cls.process_logs(logs)
        processed_logs["throughput_data"] = throughput_data
        
        return processed_logs

    @classmethod
    def process_logs(cls, logs):
        # Move up to the top of the file
        from collections import defaultdict
        import json
        import re
        from datetime import datetime
        import logging

        logger = logging.getLogger(__name__)

        error_counts = defaultdict(int)
        error_timestamps = defaultdict(lambda: defaultdict(int))
        timestamp_format = "%Y-%m-%d %H:%M:%S"
        retrieval_scores = [] # Initialize dict on different keys?
        query_timestamps = []
        vector_search_latencies = []
        rag_generation_latencies = []
        throughput_data = []

        for log in logs:
            if log["key"] == "error":
                error_message = log["value"]
                # Extract the most specific error code (the last numeric segment)
                if error_codes := re.findall(r'\b\d{3}\b', error_message):
                    error_type = error_codes[-1]
                else:
                    continue

                timestamp = datetime.strptime(log["timestamp"], timestamp_format).date()

                error_counts[error_type] += 1
                error_timestamps[timestamp][error_type] += 1

            if log["key"] == "search_results":
                results = log["value"]
                # Histogram over the max similarity, as aggregate is not necessarily interpretable and do aggregate statistics, more informative
                try:
                    results_list = json.loads(results)
                    for result_str in results_list:
                        result = json.loads(result_str)
                        score = result["score"]
                        retrieval_scores.append(score)
                except Exception as e:
                    logger.error(f"Error parsing search results: {results} - {e}")
            elif log["key"] == "search_query":
                # Expand run_info class to include arbitrary filters
                # Atomic widgets to get filtered run ids, then calculate statistics over them
                query_timestamps.append(log["timestamp"])
            elif log["key"] == "vector_search_latency":
                vector_search_latencies.append(float(log["value"]))
            elif log["key"] == "rag_generation_latency":
                rag_generation_latencies.append(float(log["value"]))
            elif log["key"] == "throughput":
                timestamp = log["timestamp"]
                num_requests = int(log["value"]["num_requests"])
                request_type = log["value"]["request_type"]
                throughput_data.append({"timestamp": timestamp, "num_requests": num_requests, "request_type": request_type})

        # Prepare data for stacked bar chart (error rates)
        stacked_bar_data = []
        for timestamp, error_dict in sorted(error_timestamps.items()):
            entry = {"timestamp": str(timestamp)}
            entry |= error_dict
            stacked_bar_data.append(entry)

        # Prepare data for pie chart (error distribution)
        pie_chart_data = [{"error_type": error_type, "count": count} for error_type, count in error_counts.items()]

        return {
            "error_rates": {
                "stackedBarChartData": {
                    "labels": [str(timestamp) for timestamp in error_timestamps.keys()],
                    "datasets": [
                        {
                            "label": f"Error Code {error_type}",
                            "data": [error_dict.get(error_type, 0) for error_dict in error_timestamps.values()]
                        } for error_type in error_counts.keys()
                    ]
                }
            },
            "error_distribution": {
                "pieChartData": pie_chart_data
            },
            "retrieval_scores": retrieval_scores,
            "query_timestamps": query_timestamps,
            "vector_search_latencies": vector_search_latencies,
            "rag_generation_latencies": rag_generation_latencies,
            "throughput_data": throughput_data,
        }
