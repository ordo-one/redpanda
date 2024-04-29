# Copyright 2023 Redpanda Data, Inc.
#
# Use of this software is governed by the Business Source License
# included in the file licenses/BSL.md
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0

import math
from time import sleep
from typing import Any, TypeVar

from rptest.services.cluster import cluster
from rptest.tests.redpanda_cloud_test import RedpandaCloudTest
from rptest.tests.redpanda_test import RedpandaTest
from ducktape.tests.test import TestContext
from rptest.services.producer_swarm import ProducerSwarm
from rptest.clients.rpk import RpkTool
from rptest.services.redpanda_cloud import CloudTierName, ProductInfo, get_config_profile_name
from rptest.services.redpanda import (SISettings, RedpandaServiceCloud)
from rptest.services.openmessaging_benchmark import OpenMessagingBenchmark
from rptest.services.openmessaging_benchmark_configs import \
    OMBSampleConfigurations
from rptest.services.machinetype import get_machine_info

KiB = 1024
MiB = KiB * KiB
GiB = KiB * MiB
KB = 10**3
MB = 10**6
GB = 10**9
minutes = 60
hours = 60 * minutes

T = TypeVar('T')


def not_none(value: T | None) -> T:
    if value is None:
        raise ValueError(f'value was unexpectedly None')
    return value


class OMBValidationTest(RedpandaCloudTest):

    # The numbers of nodes we expect to run with - this value (10) is the default
    # for duck.py so these tests should just work with that default, but not necessarily
    # any less than that.
    CLUSTER_NODES = 10

    # common workload details shared among most/all test methods
    WORKLOAD_DEFAULTS = {
        "topics": 1,
        "message_size": 1 * KiB,
        "payload_file": "payload/payload-1Kb.data",
        "consumer_backlog_size_GB": 0,
        "test_duration_minutes": 5,
        "warmup_duration_minutes": 5,
    }

    EXPECTED_MAX_LATENCIES = {
        OMBSampleConfigurations.E2E_LATENCY_50PCT: 20.0,
        OMBSampleConfigurations.E2E_LATENCY_75PCT: 25.0,
        OMBSampleConfigurations.E2E_LATENCY_99PCT: 60.0,
        OMBSampleConfigurations.E2E_LATENCY_999PCT: 100.0,
    }

    def __init__(self, test_ctx: TestContext, *args, **kwargs):
        self._ctx = test_ctx

        super().__init__(test_ctx, *args, **kwargs)

        # Load install pack and check profile
        install_pack = self.redpanda.get_install_pack()
        self.logger.info(f"Loaded install pack '{install_pack['version']}': "
                         f"Redpanda v{install_pack['redpanda_version']}, "
                         f"created at '{install_pack['created_at']}'")
        if self.config_profile_name not in install_pack['config_profiles']:
            # throw user friendly error
            _profiles = ", ".join(
                [f"'{k}'" for k in install_pack['config_profiles']])
            raise RuntimeError(
                f"'{self.config_profile_name}' not found among config profiles: {_profiles}"
            )
        config_profile = install_pack['config_profiles'][
            self.config_profile_name]

        self.num_brokers = config_profile['nodes_count']
        self.tier_limits: ProductInfo = not_none(self.redpanda.get_product())
        self.tier_machine_info = get_machine_info(
            config_profile['machine_type'])
        self.rpk = RpkTool(self.redpanda)

    def setup(self):
        super().setup()
        self.redpanda.clean_cluster()

    def tearDown(self):
        super().tearDown()
        self.redpanda.clean_cluster()

    @staticmethod
    def base_validator(multiplier: float = 1) -> dict[str, Any]:
        """Return a validator object with reasonable latency targets for
        healthy systems. Optionally accepts a multiplier value which will multiply
        all the latencies by the given value, which could be used to accept higher
        latencies in cases we know this is reasonable (e.g., a system running at
        its maximum partition count."""

        # use dict comprehension to generate dict of latencies to list of validation functions
        # e.g. { 'aggregatedEndToEndLatency50pct': [OMBSampleConfigurations.lte(20.0 * multiplier)] }
        return {
            k: [OMBSampleConfigurations.lte(v * multiplier)]
            for k, v in OMBValidationTest.EXPECTED_MAX_LATENCIES.items()
        }

    def _partition_count(self) -> int:
        machine_config = self.tier_machine_info
        return 5 * self.num_brokers * machine_config.num_shards

    def _producer_count(self, ingress_rate) -> int:
        """Determine the number of producers based on the ingress rate.
        We assume that each producer is capable of 5 MB/s."""
        return max(ingress_rate // (5 * MB), 1)

    def _consumer_count(self, egress_rate) -> int:
        """Determine the number of consumers based on the egress rate.
        We assume that each consumer is capable of 5 MB/s."""
        return max(egress_rate // (5 * MB), 1)

    def _mb_to_mib(self, mb):
        return math.floor(0.9537 * mb)

    @cluster(num_nodes=CLUSTER_NODES)
    def test_max_connections(self):
        tier_limits = self.tier_limits

        # Constants
        #

        PRODUCER_TIMEOUT_MS = 5000
        OMB_WORKERS = 2
        SWARM_WORKERS = 7

        # OMB parameters
        #

        producer_rate = tier_limits.max_ingress // 5
        subscriptions = max(tier_limits.max_egress // tier_limits.max_ingress,
                            1)
        omb_producer_count = self._producer_count(producer_rate)
        omb_consumer_count = self._consumer_count(producer_rate *
                                                  subscriptions)
        warmup_duration: int = self.WORKLOAD_DEFAULTS[
            "warmup_duration_minutes"]
        test_duration: int = self.WORKLOAD_DEFAULTS["test_duration_minutes"]

        workload = self.WORKLOAD_DEFAULTS | {
            "name":
            "MaxConnectionsTestWorkload",
            "partitions_per_topic":
            self._partition_count(),
            "subscriptions_per_topic":
            subscriptions,
            "consumer_per_subscription":
            max(omb_consumer_count // subscriptions, 1),
            "producers_per_topic":
            omb_producer_count,
            "producer_rate":
            producer_rate // (1 * KiB),
        }

        driver = {
            "name": "MaxConnectionsTestDriver",
            "replication_factor": 3,
            "request_timeout": 300000,
            "producer_config": {
                "enable.idempotence": "true",
                "acks": "all",
                "linger.ms": 1,
                "max.in.flight.requests.per.connection": 5,
            },
            "consumer_config": {
                "auto.offset.reset": "earliest",
                "enable.auto.commit": "false",
            },
        }

        validator = self.base_validator() | {
            OMBSampleConfigurations.AVG_THROUGHPUT_MBPS: [
                OMBSampleConfigurations.gte(
                    self._mb_to_mib(producer_rate // (1 * MB))),
            ],
        }

        # ProducerSwarm parameters
        #

        record_size = 64

        # estimated number of connections for OMB
        omb_connections = 3 * (omb_producer_count + omb_consumer_count)
        # The remainder of our connection budget after OMB connections are accounted for we will
        # fill with swarm connections: we add 10% to the nominal amount to ensure we test the
        # advertised limit and this uses up ~half the slack we have in the enforcement (we currently
        # set the per broker limit to 1.2x of what it would be if enforced exactly).
        swarm_target_connections = int(
            (tier_limits.max_connection_count - omb_connections) * 1.1)

        # we expect each swarm producer to create 1 connection per broker, plus 1 additional connection
        # for metadata
        conn_per_swarm_producer = self.num_brokers + 1

        producer_per_swarm_node = swarm_target_connections // conn_per_swarm_producer // SWARM_WORKERS

        msg_rate_per_node = (1 * KiB) // record_size
        messages_per_sec_per_producer = max(
            msg_rate_per_node // producer_per_swarm_node, 1)

        # single producer runtime
        # Each swarm will throttle the client creation rate to about 30 connections/second
        warm_up_time_s = producer_per_swarm_node // 30 + 60
        target_runtime_s = 60 * (test_duration +
                                 warmup_duration) + warm_up_time_s
        records_per_producer = messages_per_sec_per_producer * target_runtime_s

        total_target = omb_connections + swarm_target_connections

        self.logger.warn(
            f"Target connections: {total_target} "
            f"(OMB: {omb_connections}, swarm: {swarm_target_connections}), per-broker: {total_target / self.num_brokers}"
        )

        self.logger.warn(
            f"target_runtime: {target_runtime_s / 60}, omb test_duration: {test_duration}m, "
            f"warmup_duration: {warmup_duration}m, {warm_up_time_s / 60}m")

        self.logger.warn(
            f"OMB nodes: {OMB_WORKERS}, omb producers: {omb_producer_count}, omb consumers: "
            f"{omb_consumer_count}, producer rate: {producer_rate / 10**6} MB/s"
        )

        self.logger.warn(
            f"Swarm nodes: {SWARM_WORKERS}, producers per node: {producer_per_swarm_node}, messages per producer: "
            f"{records_per_producer} Message rate: {messages_per_sec_per_producer} msg/s"
        )

        benchmark = OpenMessagingBenchmark(self._ctx,
                                           self.redpanda,
                                           driver, (workload, validator),
                                           num_workers=OMB_WORKERS,
                                           topology="ensemble")

        # Create topic for swarm workers after OMB to avoid the reset
        swarm_topic_name = "swarm_topic"
        try:
            self.rpk.delete_topic(swarm_topic_name)
        except:
            # Ignore the exception that is thrown if the topic doesn't exist.
            pass

        self.rpk.create_topic(swarm_topic_name,
                              self._partition_count(),
                              replicas=3)

        def make_swarm():
            return ProducerSwarm(
                self._ctx,
                self.redpanda,
                topic=swarm_topic_name,
                producers=producer_per_swarm_node,
                records_per_producer=records_per_producer,
                timeout_ms=PRODUCER_TIMEOUT_MS,
                min_record_size=record_size,
                max_record_size=record_size,
                messages_per_second_per_producer=messages_per_sec_per_producer)

        swarm = [make_swarm() for _ in range(SWARM_WORKERS)]

        for s in swarm:
            s.start()

        # Allow time for the producers in the swarm to authenticate and start
        self.logger.info(
            f"waiting {warm_up_time_s} seconds for producer swarm to start")
        sleep(warm_up_time_s)

        benchmark.start()
        benchmark_time_min = benchmark.benchmark_time() + 5

        try:
            benchmark.wait(timeout_sec=benchmark_time_min * 60)

            benchmark.check_succeed()

            for s in swarm:
                s.wait(timeout_sec=30 * 60)

        finally:
            self.rpk.delete_topic(swarm_topic_name)

        self.redpanda.assert_cluster_is_reusable()

    def _warn_metrics(self, metrics, validator):
        """Validates metrics and just warn if any fail."""

        assert len(validator) > 0, "At least one metric should be validated"

        results = []
        kv_str = lambda k, v: f"Metric {k}, value {v}, "

        for key in validator.keys():
            assert key in metrics, f"Missing requested validator key {key} in metrics"

            val = metrics[key]
            for rule in validator[key]:
                if not rule[0](val):
                    results.append(kv_str(key, val) + rule[1])

        if len(results) > 0:
            self.logger.warn(str(results))

    @cluster(num_nodes=CLUSTER_NODES)
    def test_max_partitions(self):
        tier_limits = self.tier_limits

        # multiplier for the latencies to log warnings on, but still pass the test
        # because we expect poorer performance when we max out one dimension
        fudge_factor = 2.0

        # Producer clients perform poorly with many partitions. Hence we limit
        # the max amount per producer by splitting them over multiple topics.
        MAX_PARTITIONS_PER_TOPIC = 5000
        topics = math.ceil(tier_limits.max_partition_count /
                           MAX_PARTITIONS_PER_TOPIC)

        partitions_per_topic = math.ceil(tier_limits.max_partition_count /
                                         topics)
        subscriptions = max(tier_limits.max_egress // tier_limits.max_ingress,
                            1)
        producer_rate = tier_limits.max_ingress // 2
        total_producers = self._producer_count(producer_rate)
        total_consumers = self._consumer_count(producer_rate * subscriptions)

        workload = self.WORKLOAD_DEFAULTS | {
            "name":
            "MaxPartitionsTestWorkload",
            "topics":
            topics,
            "partitions_per_topic":
            partitions_per_topic,
            "subscriptions_per_topic":
            subscriptions,
            "consumer_per_subscription":
            max(total_consumers // subscriptions // topics, 1),
            "producers_per_topic":
            max(total_producers // topics, 1),
            "producer_rate":
            producer_rate / (1 * KiB),
        }

        # validator to check metrics and fail on
        fail_validator = self.base_validator(fudge_factor) | {
            OMBSampleConfigurations.AVG_THROUGHPUT_MBPS: [
                OMBSampleConfigurations.gte(
                    self._mb_to_mib(producer_rate // (1 * MB))),
            ],
        }

        # validator to check metrics and just log warning on
        warn_validator = self.base_validator() | {
            OMBSampleConfigurations.AVG_THROUGHPUT_MBPS: [
                OMBSampleConfigurations.gte(
                    self._mb_to_mib(producer_rate // (1 * MB))),
            ],
        }

        benchmark = OpenMessagingBenchmark(
            self._ctx,
            self.redpanda,
            "ACK_ALL_GROUP_LINGER_1MS_IDEM_MAX_IN_FLIGHT",
            (workload, fail_validator),
            num_workers=self.CLUSTER_NODES - 1,
            topology="ensemble")
        benchmark.start()
        benchmark_time_min = benchmark.benchmark_time() + 5
        benchmark.wait(timeout_sec=benchmark_time_min * 60)

        # check if omb gave errors, but don't process metrics
        benchmark.check_succeed(validate_metrics=False)

        # benchmark.metrics has a lot of measurements,
        # so just get the measurements specified in EXPECTED_MAX_LATENCIES
        # using dict comprehension
        latency_metrics = {
            k: benchmark.metrics[k]
            for k in OMBValidationTest.EXPECTED_MAX_LATENCIES.keys()
        }
        self.logger.info(f'latency_metrics: {latency_metrics}')

        # just warn on the latency if above expected
        self._warn_metrics(benchmark.metrics, warn_validator)

        # fail test if the latency is above expected including fudge factor
        benchmark.check_succeed()

        self.redpanda.assert_cluster_is_reusable()

    @cluster(num_nodes=CLUSTER_NODES)
    def test_common_workload(self):
        tier_limits = self.tier_limits

        subscriptions = max(tier_limits.max_egress // tier_limits.max_ingress,
                            1)
        partitions = self._partition_count()
        total_producers = self._producer_count(tier_limits.max_ingress)
        total_consumers = self._consumer_count(tier_limits.max_egress)
        validator = self.base_validator() | {
            OMBSampleConfigurations.AVG_THROUGHPUT_MBPS: [
                OMBSampleConfigurations.gte(
                    self._mb_to_mib(tier_limits.max_ingress // (1 * MB))),
            ],
        }

        workload = self.WORKLOAD_DEFAULTS | {
            "name": "CommonTestWorkload",
            "partitions_per_topic": partitions,
            "subscriptions_per_topic": subscriptions,
            "consumer_per_subscription": max(total_consumers // subscriptions,
                                             1),
            "producers_per_topic": total_producers,
            "producer_rate": tier_limits.max_ingress // (1 * KiB),
        }

        driver = {
            "name": "CommonTestDriver",
            "reset": "true",
            "replication_factor": 3,
            "request_timeout": 300000,
            "producer_config": {
                "enable.idempotence": "true",
                "acks": "all",
                "linger.ms": 1,
                "max.in.flight.requests.per.connection": 5,
            },
            "consumer_config": {
                "auto.offset.reset": "earliest",
                "enable.auto.commit": "false",
            },
        }

        benchmark = OpenMessagingBenchmark(self._ctx,
                                           self.redpanda,
                                           driver, (workload, validator),
                                           num_workers=self.CLUSTER_NODES - 1,
                                           topology="ensemble")
        benchmark.start()
        benchmark_time_min = benchmark.benchmark_time() + 5
        benchmark.wait(timeout_sec=benchmark_time_min * 60)
        benchmark.check_succeed()
        self.redpanda.assert_cluster_is_reusable()

    @cluster(num_nodes=CLUSTER_NODES)
    def test_retention(self):
        tier_limits = self.tier_limits

        subscriptions = max(tier_limits.max_egress // tier_limits.max_ingress,
                            1)
        producer_rate = tier_limits.max_ingress
        partitions = self._partition_count()
        segment_bytes = 64 * MiB
        retention_bytes = 2 * segment_bytes
        # This will have 1/2 the test run with segment deletion occuring.
        test_duration_seconds = max(
            (2 * retention_bytes * partitions) // producer_rate, 5 * 60)

        total_producers = self._producer_count(producer_rate)
        total_consumers = self._consumer_count(producer_rate * subscriptions)

        workload = self.WORKLOAD_DEFAULTS | {
            "name": "RetentionTestWorkload",
            "partitions_per_topic": partitions,
            "subscriptions_per_topic": subscriptions,
            "consumer_per_subscription": max(total_consumers // subscriptions,
                                             1),
            "producers_per_topic": total_producers,
            "producer_rate": producer_rate // (1 * KiB),
            "test_duration_minutes": test_duration_seconds // 60,
        }

        driver = {
            "name": "RetentionTestDriver",
            "replication_factor": 3,
            "request_timeout": 300000,
            "producer_config": {
                "enable.idempotence": "true",
                "acks": "all",
                "linger.ms": 1,
                "max.in.flight.requests.per.connection": 5,
            },
            "consumer_config": {
                "auto.offset.reset": "earliest",
                "enable.auto.commit": "false",
            },
            "topic_config": {
                "retention.bytes": retention_bytes,
                "retention.local.target.bytes": retention_bytes,
                "segment.bytes": segment_bytes,
            },
        }

        validator = self.base_validator() | {
            OMBSampleConfigurations.AVG_THROUGHPUT_MBPS: [
                OMBSampleConfigurations.gte(
                    self._mb_to_mib(producer_rate // (1 * MB))),
            ],
        }

        benchmark = OpenMessagingBenchmark(self._ctx,
                                           self.redpanda,
                                           driver, (workload, validator),
                                           num_workers=self.CLUSTER_NODES - 1,
                                           topology="ensemble")
        benchmark.start()
        benchmark_time_min = benchmark.benchmark_time() + 5
        benchmark.wait(timeout_sec=benchmark_time_min * 60)
        benchmark.check_succeed()
        self.redpanda.assert_cluster_is_reusable()
