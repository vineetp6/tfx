# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for tfx.dsl.compiler.compiler_utils."""
import itertools

import tensorflow as tf
from tfx import types
from tfx.components import CsvExampleGen
from tfx.components import StatisticsGen
from tfx.dsl.compiler import compiler_utils
from tfx.dsl.components.base import base_component
from tfx.dsl.components.base import base_executor
from tfx.dsl.components.base import executor_spec
from tfx.dsl.components.common import importer
from tfx.dsl.components.common import resolver
from tfx.dsl.input_resolution.strategies import latest_blessed_model_strategy
from tfx.dsl.placeholder import placeholder as ph
from tfx.orchestration import pipeline
from tfx.proto.orchestration import pipeline_pb2
from tfx.types import standard_artifacts
from tfx.types.artifact import Artifact
from tfx.types.artifact import Property
from tfx.types.artifact import PropertyType
from tfx.types.channel import Channel
from tfx.types.channel import OutputChannel
from tfx.types.channel_utils import external_pipeline_artifact_query

from google.protobuf import text_format
from ml_metadata.proto import metadata_store_pb2


class EmptyComponentSpec(types.ComponentSpec):
  PARAMETERS = {}
  INPUTS = {}
  OUTPUTS = {}


class EmptyComponent(base_component.BaseComponent):

  SPEC_CLASS = EmptyComponentSpec
  EXECUTOR_SPEC = executor_spec.ExecutorClassSpec(base_executor.BaseExecutor)

  def __init__(self, name):
    super().__init__(spec=EmptyComponentSpec())
    self._id = name


class _MyType(Artifact):
  TYPE_NAME = "MyTypeName"
  PROPERTIES = {
      "string_value": Property(PropertyType.STRING),
  }


class CompilerUtilsTest(tf.test.TestCase):

  def testSetRuntimeParameterPb(self):
    pb = pipeline_pb2.RuntimeParameter()
    compiler_utils.set_runtime_parameter_pb(pb, "test_name", str,
                                            "test_default_value")
    expected_pb = pipeline_pb2.RuntimeParameter(
        name="test_name",
        type=pipeline_pb2.RuntimeParameter.Type.STRING,
        default_value=metadata_store_pb2.Value(
            string_value="test_default_value"))
    self.assertEqual(expected_pb, pb)

  def testSetStructuralRuntimeParameterPb(self):
    pb = compiler_utils.set_structural_runtime_parameter_pb(
        pipeline_pb2.StructuralRuntimeParameter(),
        ["pipeline_name", ("pipeline-run-id", str, "default_pipeline_run_id")])

    expected_pb = pipeline_pb2.StructuralRuntimeParameter(parts=[
        pipeline_pb2.StructuralRuntimeParameter.StringOrRuntimeParameter(
            constant_value="pipeline_name"),
        pipeline_pb2.StructuralRuntimeParameter.StringOrRuntimeParameter(
            runtime_parameter=pipeline_pb2.RuntimeParameter(
                name="pipeline-run-id",
                type=pipeline_pb2.RuntimeParameter.Type.STRING,
                default_value=metadata_store_pb2.Value(
                    string_value="default_pipeline_run_id")))
    ])
    self.assertEqual(expected_pb, pb)

  def testIsResolver(self):
    resv = resolver.Resolver(
        strategy_class=latest_blessed_model_strategy.LatestBlessedModelStrategy)
    self.assertTrue(compiler_utils.is_resolver(resv))

    example_gen = CsvExampleGen(input_base="data_path")
    self.assertFalse(compiler_utils.is_resolver(example_gen))

  def testIsImporter(self):
    impt = importer.Importer(
        source_uri="uri/to/schema", artifact_type=standard_artifacts.Schema)
    self.assertTrue(compiler_utils.is_importer(impt))

    example_gen = CsvExampleGen(input_base="data_path")
    self.assertFalse(compiler_utils.is_importer(example_gen))

  def testEnsureTopologicalOrder(self):
    a = EmptyComponent(name="a")
    b = EmptyComponent(name="b")
    c = EmptyComponent(name="c")
    a.add_downstream_node(b)
    a.add_downstream_node(c)
    valid_orders = {"abc", "acb"}
    for order in itertools.permutations([a, b, c]):
      if "".join([c.id for c in order]) in valid_orders:
        self.assertTrue(compiler_utils.ensure_topological_order(order))
      else:
        self.assertFalse(compiler_utils.ensure_topological_order(order))

  def testIncompatibleExecutionMode(self):
    p = pipeline.Pipeline(
        pipeline_name="fake_name",
        pipeline_root="fake_root",
        enable_cache=True,
        execution_mode=pipeline.ExecutionMode.ASYNC)

    with self.assertRaisesRegex(RuntimeError, "Caching is a feature only"):
      compiler_utils.resolve_execution_mode(p)

  def testHasTaskDependency(self):
    example_gen = CsvExampleGen(input_base="data_path")
    statistics_gen = StatisticsGen(examples=example_gen.outputs["examples"])
    p1 = pipeline.Pipeline(
        pipeline_name="fake_name",
        pipeline_root="fake_root",
        components=[example_gen, statistics_gen])
    self.assertFalse(compiler_utils.has_task_dependency(p1))

    a = EmptyComponent(name="a").with_id("a")
    statistics_gen.add_downstream_node(a)
    p2 = pipeline.Pipeline(
        pipeline_name="fake_name",
        pipeline_root="fake_root",
        components=[example_gen, statistics_gen, a])
    self.assertTrue(compiler_utils.has_task_dependency(p2))

  def testNodeContextName(self):
    self.assertEqual(
        "pipeline_context_name.node_id",
        compiler_utils.node_context_name("pipeline_context_name", "node_id"))

  def testImplicitChannelKey(self):
    model = types.Channel(
        type=standard_artifacts.Model,
        producer_component_id="trainer",
        output_key="model")
    self.assertEqual("_trainer.model",
                     compiler_utils.implicit_channel_key(model))

    external_pipeline_channel = external_pipeline_artifact_query(
        artifact_type=standard_artifacts.Model,
        owner="owner",
        pipeline_name="pipeline_name",
        producer_component_id="trainer",
        output_key="model",
    )
    self.assertEqual(
        "_trainer.model.owner.pipeline_name",
        compiler_utils.implicit_channel_key(external_pipeline_channel),
    )

  def testBuildChannelToKeyFn(self):
    model = types.Channel(
        type=standard_artifacts.Model,
        producer_component_id="trainer",
        output_key="model")
    examples = types.Channel(
        type=standard_artifacts.Examples,
        producer_component_id="example_gen",
        output_key="examples")

    fn = compiler_utils.build_channel_to_key_fn({"_trainer.model": "real_key"})
    self.assertEqual(fn(model), "real_key")
    self.assertEqual(fn(examples), "_example_gen.examples")


class ValidateExecPropertyPlaceholderTest(tf.test.TestCase):

  def test_accepts_canonical_dynamic_exec_prop_placeholder(self):
    # .future()[0].uri is how we tell users to hook up a dynamic exec prop.
    compiler_utils.validate_exec_property_placeholder(
        "testkey", Channel(type=_MyType).future()[0].value
    )

  def test_accepts_complex_exec_prop_placeholder(self):
    compiler_utils.validate_exec_property_placeholder(
        "testkey",
        ph.execution_invocation().pipeline_run_id
        + "foo"
        + ph.input("someartifact").uri
        + "/somefile.txt",
    )

  def test_accepts_complex_dynamic_exec_prop_placeholder(self):
    compiler_utils.validate_exec_property_placeholder(
        "testkey",
        Channel(type=_MyType).future()[0].value
        + "foo"
        + ph.input("someartifact").uri
        + "/somefile.txt",
    )

  def test_rejects_output_artifact_placeholder(self):
    with self.assertRaisesRegex(
        ValueError, ".*testkey.*output placeholder.*someartifact.*"
    ):
      compiler_utils.validate_exec_property_placeholder(
          "testkey", ph.output("someartifact").uri
      )
    with self.assertRaisesRegex(
        ValueError, ".*testkey.*output placeholder.*someartifact.*"
    ):
      compiler_utils.validate_exec_property_placeholder(
          "testkey",
          ph.execution_invocation().pipeline_run_id
          + "foo"
          + ph.output("someartifact").uri
          + "/somefile.txt",
      )

  def test_rejects_exec_property_dependency(self):
    # One exec property can't depend on another. And we're validating
    # placeholders that will populate exec properties here, so they can't read
    # from them.
    with self.assertRaisesRegex(
        ValueError, ".*testkey.*another exec property.*somekey"
    ):
      compiler_utils.validate_exec_property_placeholder(
          "testkey", ph.exec_property("somekey")
      )
    with self.assertRaisesRegex(
        ValueError, ".*testkey.*another exec property.*somekey"
    ):
      compiler_utils.validate_exec_property_placeholder(
          "testkey",
          ph.execution_invocation().pipeline_run_id
          + "foo"
          + ph.exec_property("somekey")
          + "/somefile.txt",
      )

  def testOutputSpecFromChannel_AsyncOutputChannel(self):
    channel = OutputChannel(
        artifact_type=standard_artifacts.Model,
        output_key="model",
        producer_component="trainer",
        is_async=True,
    )

    actual = compiler_utils.output_spec_from_channel(channel, "trainer")
    expected = text_format.Parse(
        """
        artifact_spec {
          type {
            name: "Model"
            base_type: MODEL
          }
          is_async: true
        }
        """,
        pipeline_pb2.OutputSpec(),
    )
    self.assertProtoEquals(actual, expected)


if __name__ == "__main__":
  tf.test.main()
