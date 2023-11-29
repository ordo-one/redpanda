# Copyright 2023 Redpanda Data, Inc.
#
# Use of this software is governed by the Business Source License
# included in the file licenses/BSL.md
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0
import json
import logging
import os
import random
import string
import sys
import threading
import time

from copy import deepcopy
from dataclasses import dataclass
from typing import Dict, Union, List

from pyflink.common import Types, Time
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.connectors.kafka import FlinkKafkaProducer, \
    FlinkKafkaConsumer, DeserializationSchema
from pyflink.datastream.formats.json import JsonRowSerializationSchema, \
    JsonRowDeserializationSchema
from pyflink.datastream.functions import KeyedProcessFunction, RuntimeContext
from pyflink.datastream.state import ValueStateDescriptor, StateTtlConfig


@ dataclass(kw_only=True)
class WorkloadConfig:
    # Default values are set for CDT run inside EC2 instance
    connector_path: str = "file:///opt/flink/connectors/flink-sql-connector-kafka-3.0.1-1.18.jar",
    logger_path: str = "/opt/flink/log",
    log_level: str = "DEBUG",
    producer_group: str = "flink_produce_group",
    consumer_group: str = "flink_consume_group",
    topic_name: str = "flink_workload_topic",
    msg_size: int = 4096,
    count: int = 1000
    # This should be updated to value from self.redpanda.brokers()
    brokers: str = "localhost:9092",


def setup_logger(path, level):
    # Simple file logger
    filename = f"{os.path.basename(__file__).split('.')[0]}.log"
    handler = logging.FileHandler(os.path.join(path, filename))
    handler.setFormatter(
        logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    level = logging.getLevelName(level.upper())
    handler.setLevel(level)
    logger = logging.getLogger(__name__)
    logger.addHandler(handler)

    return logger


class MessageConsumer(KeyedProcessFunction):
    """
        Simple consumer based on Apache example.
        Receives a message and counts characters
    """
    def __init__(self, logger):
        self.logger = logger
        self.state = None
        self._message_count = 0
        self.last_message_time = time.time()

    def open(self, runtime_context: RuntimeContext):
        self.logger.info("MessageConsumer process started")
        # Create descriptor for the saved state
        state_descriptor = ValueStateDescriptor("character_count", Types.INT())
        state_ttl_config = StateTtlConfig \
            .new_builder(Time.seconds(1)) \
            .set_update_type(StateTtlConfig.UpdateType.OnReadAndWrite) \
            .disable_cleanup_in_background() \
            .build()
        state_descriptor.enable_time_to_live(state_ttl_config)
        self.state = runtime_context.get_state(state_descriptor)

    def process_element(self, value, ctx: 'KeyedProcessFunction.Context'):
        # retrieve the current character count
        current = self.state.value()
        if current is None:
            current = 0

        # update and save
        # Count chars and add it to total
        chars = len(value[1])
        current += chars
        # save it
        self.state.update(current)
        # Log and save time received
        self.logger.debug(f"...received {chars} chars, total: {current}")
        self.time = time.time()

        # increment message count
        self._message_count += 1

        # return
        yield value[0], current

    def get_message_count(self):
        return self._message_count


class MyKafkaConsumer(FlinkKafkaConsumer):
    """
        Wrapper class for FlinkKafkaConsumer.
        Has additional methods to hold message consumer
        and a wrapped 'run' method
    """
    def __init__(self,
                 logger,
                 topics: Union[str, List[str]] = "test_topic",
                 deserialization_schema: DeserializationSchema = None,
                 properties: Dict = {},
                 message_count: int = 0,
                 timeout: int = 120):
        super(MyKafkaConsumer, self).__init__(
            topics=topics,
            deserialization_schema=deserialization_schema,
            properties=properties)
        self.message_count = message_count
        self.timeout = timeout
        self.logger = logger
        # Initialize MessageConsumer internally
        self.consumer = MessageConsumer(logger)
        # Do some initialization
        self.set_start_from_earliest()
        self.run_thread = None

    def get_message_consumer(self):
        return self.consumer

    def run(self):
        # Instead of calling base function 'run', wrap it
        if self.run_thread is not None:
            self.run_thread = threading.Thread(target=self.run,
                                               args=None,
                                               kwargs=None)
            self.run_thread.start()

        # check if message count or timeout in consumer is reached
        _now = time.time()
        if self.message_count <= self.consumer.get_message_count() or \
                _now - self.consumer.last_message_time > self.timeout:
            self.run_thread.get_java_function().cancel()
            self.run_thread.join()

        return


class FlinkWorkload:
    def __init__(self, config_override):
        self.config = WorkloadConfig(**config_override)
        self.logger = setup_logger(self.config.logger_path,
                                   self.config.log_level)

    def setup(self):
        # Initialize
        self.env = StreamExecutionEnvironment.get_execution_environment()
        self.env.add_jars(self.config.connector_path)
        self._basic_properties = {
            'bootstrap.servers': self.config.brokers,
        }
        self.type_info = Types.ROW([Types.STRING()])

        self.messages = []

    def _generate_message(self):
        return ''.join(random.choices(
            string.ascii_letters + string.digits, k=self.config.msg_size))

    def task_produce(self):
        """
            Example of a produce task

            Steps:
            - generate messages using configured size and random.choices() func
            - create simple serializer
            - create Producer with topic name and group
            - execute producer
        """
        # Prepare data to be sent
        _messages = [(self._generate_message(),)
                     for i in range(self.config.count)]
        ds = self.env.from_collection(_messages, type_info=self.type_info)

        # Serializer
        serialization_schema = JsonRowSerializationSchema.Builder() \
            .with_type_info(self.type_info) \
            .build()

        # Producer creation
        _properties = deepcopy(self._basic_properties)
        _properties['group.id'] = self.config.producer_group

        kafka_producer = FlinkKafkaProducer(
            topic=self.config.topic_name,
            serialization_schema=serialization_schema,
            producer_config=_properties
        )

        # Output type of ds must be RowTypeInfo
        ds.add_sink(kafka_producer)
        self.env.execute()

    def task_consume(self):
        """
            Example consume task

            Steps:
            - create deserializer and configured properties
            - create consumer
            - set offset to earliest
            - assign source and add callback class
            - execute
        """
        # Deserialization schema
        deserialization_schema = JsonRowDeserializationSchema.Builder() \
            .type_info(self.type_info) \
            .build()
        # Consumer creation
        properties = deepcopy(self._basic_properties)
        properties['group.id'] = self.config.consumer_group

        # Create my consumer
        my_consumer = MyKafkaConsumer(
            self.config.topic_name,
            deserialization_schema=deserialization_schema,
            properties=properties
        )
        # Add source and link message consumer
        self.env.add_source(my_consumer).process(
            my_consumer.get_message_consumer(), self.type_info)
        # Run
        self.env.execute()

    def run_tasks(self, tasks):
        # No tag or keyword checking as in run_all_tasks
        # Validation is ommitted at this point in time
        for task in tasks:
            self.logger.info(f"Running task '{task}'")
            try:
                task()
            except Exception as e:
                # Just log error and return
                self.logger.error(f"Task '{task}' failed: {e}")

        return

    def run_all_tasks(self):
        # Prepare tasks
        _producing = []
        _consuming = []
        _other = []
        # Dynamic method loading
        for method in dir(self):
            if method.startswith("task_"):
                if "produce" in method:
                    _producing.append(getattr(self, method))
                elif "consume" in method:
                    _consuming.append(getattr(self, method))
                else:
                    _other.append(getattr(self, method))

        # methods with keyword 'produce' goes first
        self.run_tasks(_producing)
        # all other goes in between
        self.run_tasks(_other)
        # keyword 'consume' goes last
        self.run_tasks(_consuming)

    def cleanup(self):
        # Nothing to cleanup as of right now
        pass


if __name__ == '__main__':
    # Temp log that prints to stdout
    log = logging.basicConfig(stream=sys.stdout,
                              level=logging.INFO,
                              format="%(message)s")
    # Load config if specified
    if len(sys.argv) > 1:
        # Validate arguments in a quick and dirty way.
        # This will assign one argument to filename and generate exception if
        # there is more than one argument
        try:
            [filename] = sys.argv[1:]
        except Exception:
            log.error("Wrong number of arguments."
                      "Should be one with path to flink_workload_conf.json")
            sys.exit(1)
    else:
        # No config path provided, just use defaults
        filename = "/workloads/flink_workload_config.json"

    # Load configuration
    with open(filename, 'r+t') as f:
        input_config = json.load(f)

    # No stdout messages from this point forward. Only errors.
    # Main reason, stdout should stay as clean as possible as this is to be
    # caught by ducktape and parsed for JobIds
    workload = FlinkWorkload(input_config)
    try:
        workload.setup()
        # Specific scenario run example
        # tasks = [
        #     workload.task_produce,
        #     workload.task_consume
        # ]
        # workload.run(tasks)

        # Or just all of the tasks
        workload.run_all_tasks()

    except Exception:
        # Do not re-throw not to cause a commoution
        log.error("Workload run failed")
        sys.exit(1)
    finally:
        workload.cleanup()
