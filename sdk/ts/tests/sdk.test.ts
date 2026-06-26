/**
 * Tests for TypeScript Cost Model SDK.
 *
 * Validates that the TypeScript SDK produces the same CostModel representation
 * as the Python and YAML surfaces (Principle 11 — three surfaces, one schema).
 */

import { describe, it, expect } from "vitest";
import {
  Workflow,
  NodeUsage,
  perSecond,
  perMinute,
  perHour,
  perDay,
  perWeek,
  perMonth,
  parseFrequency,
  parseYamlDsl,
} from "../src/index";
import type { CostModel } from "../src/types";

// ── Frequency Constructors ───────────────────────────────────────────────────

describe("Frequency constructors", () => {
  it("perSecond creates correct frequency", () => {
    expect(perSecond(100)).toEqual({ value: 100, unit: "perSecond" });
  });

  it("perMinute creates correct frequency", () => {
    expect(perMinute(1000)).toEqual({ value: 1000, unit: "perMinute" });
  });

  it("perHour creates correct frequency", () => {
    expect(perHour(60)).toEqual({ value: 60, unit: "perHour" });
  });

  it("perDay creates correct frequency", () => {
    expect(perDay(1)).toEqual({ value: 1, unit: "perDay" });
  });

  it("perWeek creates correct frequency", () => {
    expect(perWeek(100)).toEqual({ value: 100, unit: "perWeek" });
  });

  it("perMonth creates correct frequency", () => {
    expect(perMonth(1000)).toEqual({ value: 1000, unit: "perMonth" });
  });

  it("parseFrequency parses valid shorthand", () => {
    expect(parseFrequency("1000/min")).toEqual({
      value: 1000,
      unit: "perMinute",
    });
    expect(parseFrequency("1/sec")).toEqual({ value: 1, unit: "perSecond" });
    expect(parseFrequency("3600/hr")).toEqual({ value: 3600, unit: "perHour" });
    expect(parseFrequency("86400/day")).toEqual({
      value: 86400,
      unit: "perDay",
    });
    expect(parseFrequency("100/week")).toEqual({
      value: 100,
      unit: "perWeek",
    });
    expect(parseFrequency("1000/month")).toEqual({
      value: 1000,
      unit: "perMonth",
    });
  });

  it("parseFrequency throws on invalid format", () => {
    expect(() => parseFrequency("invalid")).toThrow();
    expect(() => parseFrequency("1000/unknown")).toThrow();
  });
});

// ── NodeUsage Builder ────────────────────────────────────────────────────────

describe("NodeUsage", () => {
  it("adds metrics fluently", () => {
    const usage = new NodeUsage()
      .withMetric("duration_ms", 200, "ms")
      .withMetric("memory_mb", 128);

    expect(usage.metrics).toEqual({
      duration_ms: { value: 200, unit: "ms" },
      memory_mb: 128,
    });
  });

  it("adds fixed (always-on) metrics alongside usage-driven ones", () => {
    const usage = new NodeUsage()
      .withFixedMetric("gateway_hours", 730, "hours")
      .withMetric("gb_processed", 2, "GB");

    expect(usage.metrics).toEqual({
      gateway_hours: { value: 730, unit: "hours", fixed: true },
      gb_processed: { value: 2, unit: "GB" },
    });
  });
});

// ── Workflow Builder ─────────────────────────────────────────────────────────

describe("Workflow", () => {
  it("builds a basic cost model", () => {
    const wf = new Workflow("my-api");
    wf.setEntry("aws_api_gatewayv2_api.gateway");
    wf.setFrequency(perMinute(1000));

    wf.addNode("aws_api_gatewayv2_api.gateway", {
      nodeType: "routing",
      resourceAddress: "aws_api_gatewayv2_api.gateway",
      provider: "aws",
      service: "AmazonAPIGatewayHTTP",
    });

    wf.addNode("aws_lambda_function.handler", {
      nodeType: "compute",
      resourceAddress: "aws_lambda_function.handler",
      provider: "aws",
      service: "AWSLambda",
    });

    wf.calls("aws_api_gatewayv2_api.gateway", [
      { to: "aws_lambda_function.handler", rate: 1 },
    ]);

    wf.usage(
      "aws_lambda_function.handler",
      new NodeUsage().withMetric("duration_ms", 200, "ms")
    );

    const model = wf.toCostModel();
    expect(model.version).toBe("1.0");
    expect(model.workflow.name).toBe("my-api");
    expect(model.workflow.entry).toBe("aws_api_gatewayv2_api.gateway");
    expect(model.workflow.frequency).toEqual({
      value: 1000,
      unit: "perMinute",
    });
    expect(model.edges).toHaveLength(1);
    expect(model.edges![0]).toEqual({
      from: "aws_api_gatewayv2_api.gateway",
      to: "aws_lambda_function.handler",
      rate: 1,
      type: "invoke",
    });
  });

  it("throws if entry not set", () => {
    const wf = new Workflow("test");
    wf.setFrequency(perMinute(1));
    expect(() => wf.toCostModel()).toThrow("entry");
  });

  it("throws if frequency not set", () => {
    const wf = new Workflow("test");
    wf.setEntry("node.x");
    expect(() => wf.toCostModel()).toThrow("frequency");
  });

  it("supports parameters for what-if analysis", () => {
    const wf = new Workflow("test");
    wf.setEntry("node.x");
    wf.setFrequency(perMinute(100));
    wf.setParameter("traffic_multiplier", 2.0);

    const model = wf.toCostModel();
    expect(model.workflow.parameters).toEqual({ traffic_multiplier: 2.0 });
  });

  it("supports flatOverride escape hatch (Principle 9)", () => {
    const wf = new Workflow("test");
    wf.setEntry("node.x");
    wf.setFrequency(perMinute(100));
    wf.addNode("node.x", {
      nodeType: "storage",
      resourceAddress: "node.x",
      provider: "aws",
    });
    wf.setFlatOverride("node.x", true);

    const model = wf.toCostModel();
    expect(model.nodes["node.x"]!.flatOverride).toBe(true);
  });

  it("produces valid JSON output", () => {
    const wf = new Workflow("test");
    wf.setEntry("node.a");
    wf.setFrequency(perSecond(1));

    const json = wf.toJSON();
    const parsed = JSON.parse(json) as CostModel;
    expect(parsed.version).toBe("1.0");
    expect(parsed.workflow.name).toBe("test");
  });

  it("omits edges when empty", () => {
    const wf = new Workflow("test");
    wf.setEntry("node.a");
    wf.setFrequency(perSecond(1));

    const model = wf.toCostModel();
    expect(model.edges).toBeUndefined();
  });

  it("supports chaining", () => {
    const wf = new Workflow("chained")
      .setEntry("gw")
      .setFrequency(perMinute(500))
      .setParameter("env", 1.0);

    expect(wf.name).toBe("chained");
    const model = wf.toCostModel();
    expect(model.workflow.entry).toBe("gw");
  });
});

// ── YAML DSL Parser ──────────────────────────────────────────────────────────

describe("parseYamlDsl", () => {
  it("parses standard YAML format", () => {
    const yaml = `
version: "1.0"
workflow:
  name: test-workflow
  entry: api_gateway
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
edges:
  - from: api_gateway
    to: lambda_func
    rate: 1
`;

    const model = parseYamlDsl(yaml);
    expect(model.workflow.name).toBe("test-workflow");
    expect(model.edges).toHaveLength(1);
  });

  it("parses shorthand frequency notation", () => {
    const yaml = `
version: "1.0"
workflow:
  name: test
  entry: gw
  frequency: "1000/min"
nodes:
  gw:
    nodeType: routing
    resourceAddress: gw
`;

    const model = parseYamlDsl(yaml);
    expect(model.workflow.frequency).toEqual({
      value: 1000,
      unit: "perMinute",
    });
  });

  it("parses shorthand frequency with week and month units", () => {
    const weekYaml = `
version: "1.0"
workflow:
  name: test-weekly
  entry: gw
  frequency: "100/week"
nodes:
  gw:
    nodeType: routing
    resourceAddress: gw
`;
    const weekModel = parseYamlDsl(weekYaml);
    expect(weekModel.workflow.frequency).toEqual({
      value: 100,
      unit: "perWeek",
    });

    const monthYaml = `
version: "1.0"
workflow:
  name: test-monthly
  entry: gw
  frequency: "1000/month"
nodes:
  gw:
    nodeType: routing
    resourceAddress: gw
`;
    const monthModel = parseYamlDsl(monthYaml);
    expect(monthModel.workflow.frequency).toEqual({
      value: 1000,
      unit: "perMonth",
    });
  });

  it("parses arrow syntax (DSL format)", () => {
    const yaml = `
version: "1.0"
workflow:
  name: dsl-test
  entry: gw
  frequency:
    unit: perMinute
    value: 100
nodes:
  gw:
    nodeType: routing
    resourceAddress: gw
calls:
  gw:
    → lambda_func: 1
`;

    const model = parseYamlDsl(yaml);
    expect(model.edges).toHaveLength(1);
    expect(model.edges![0]).toEqual({
      from: "gw",
      to: "lambda_func",
      rate: 1,
    });
  });

  it("parses arrow syntax with object value", () => {
    const yaml = `
version: "1.0"
workflow:
  name: dsl-test
  entry: gw
  frequency:
    unit: perMinute
    value: 100
nodes:
  gw:
    nodeType: routing
    resourceAddress: gw
calls:
  gw:
    → lambda_func:
      rate: 0.5
      type: read
`;

    const model = parseYamlDsl(yaml);
    expect(model.edges).toHaveLength(1);
    expect(model.edges![0]!.type).toBe("read");
    expect(model.edges![0]!.rate).toBe(0.5);
  });

  it("throws on missing workflow section", () => {
    const yaml = 'version: "1.0"\nnodes: {}';
    expect(() => parseYamlDsl(yaml)).toThrow("workflow");
  });
});

// ── Cross-Surface Compatibility ──────────────────────────────────────────────

describe("Cross-surface compatibility", () => {
  it("TypeScript output is valid JSON matching schema shape", () => {
    const wf = new Workflow("compat-test");
    wf.setEntry("aws_lambda_function.app");
    wf.setFrequency(perSecond(10));

    wf.addNode("aws_lambda_function.app", {
      nodeType: "compute",
      resourceAddress: "aws_lambda_function.app",
      provider: "aws",
      service: "AWSLambda",
      region: "us-east-1",
    });

    const model = wf.toCostModel();

    // Validate required top-level properties
    expect(model.version).toBe("1.0");
    expect(model.workflow).toBeDefined();
    expect(model.workflow.name).toBe("compat-test");
    expect(model.workflow.entry).toBeTruthy();
    expect(model.workflow.frequency).toBeDefined();
    expect(model.workflow.frequency.unit).toBe("perSecond");
    expect(model.workflow.frequency.value).toBe(10);
    expect(model.nodes).toBeDefined();

    // Validate node structure
    const node = model.nodes["aws_lambda_function.app"];
    expect(node).toBeDefined();
    expect(node!.nodeType).toBe("compute");
    expect(node!.resourceAddress).toBe("aws_lambda_function.app");
    expect(node!.provider).toBe("aws");
  });

  it("YAML DSL output is structurally identical to Workflow output", () => {
    const yaml = `
version: "1.0"
workflow:
  name: equivalence-test
  entry: api
  frequency:
    unit: perSecond
    value: 1
nodes:
  api:
    nodeType: routing
    resourceAddress: api
    provider: aws
    service: APIGateway
`;

    const fromYaml = parseYamlDsl(yaml);

    const wf = new Workflow("equivalence-test");
    wf.setEntry("api");
    wf.setFrequency(perSecond(1));
    wf.addNode("api", {
      nodeType: "routing",
      resourceAddress: "api",
      provider: "aws",
      service: "APIGateway",
    });
    const fromTs = wf.toCostModel();

    // Both should produce the same cost model representation
    expect(fromYaml.workflow).toEqual(fromTs.workflow);
    expect(fromYaml.nodes).toEqual(fromTs.nodes);
  });
});
