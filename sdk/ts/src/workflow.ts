/**
 * Workflow builder — the main entry point for the TypeScript SDK.
 *
 * Mirroring the Python SDK's Workflow class with a fluent, type-safe API
 * for declaring cost models in TypeScript.
 */

import type {
  CallConfig,
  CostModel,
  CostNode,
  Edge,
  Frequency,
  MetricValue,
} from "./types";

/** A usage builder for fluent metric declaration. */
export class NodeUsage {
  metrics: Record<string, number | { value: number; unit?: string }> = {};

  /** Add a usage metric. Returns this for chaining. */
  withMetric(name: string, value: number, unit?: string): this {
    this.metrics[name] = unit ? { value, unit } : value;
    return this;
  }
}

/**
 * Cost model workflow definition.
 *
 * The main entry point for declaring cost models in TypeScript.
 * Produces a CostModel representation that can be serialized to JSON
 * and consumed by the cost engine.
 *
 * @example
 * ```typescript
 * const wf = new Workflow("my-api");
 * wf.setEntry("aws_api_gatewayv2_api.my_api");
 * wf.setFrequency(perMinute(1000));
 *
 * wf.calls("aws_api_gatewayv2_api.my_api", [
 *   { to: "aws_lambda_function.handler", rate: 1 },
 * ]);
 *
 * wf.usage("aws_lambda_function.handler",
 *   new NodeUsage().withMetric("duration_ms", 200, "ms")
 * );
 *
 * const model = wf.toCostModel();
 * ```
 */
export class Workflow {
  private _name: string;
  private _entry: string | null = null;
  private _frequency: Frequency | null = null;
  private _parameters: Record<string, number> = {};
  private _nodes: Record<string, Record<string, unknown>> = {};
  private _edges: Edge[] = [];

  constructor(name: string) {
    this._name = name;
  }

  /** Set the entry node resource address. */
  setEntry(address: string): this {
    this._entry = address;
    return this;
  }

  /** Set the workflow frequency. */
  setFrequency(freq: Frequency): this {
    this._frequency = freq;
    return this;
  }

  /** Set a named parameter for what-if analysis. */
  setParameter(name: string, value: number): this {
    this._parameters[name] = value;
    return this;
  }

  /** Add nodes (typically auto-extracted from IaC). */
  addNodes(nodes: Record<string, CostNode>): this {
    Object.assign(this._nodes, nodes);
    return this;
  }

  /** Add a single node. */
  addNode(address: string, node: CostNode): this {
    this._nodes[address] = node as unknown as Record<string, unknown>;
    return this;
  }

  /** Define outgoing call edges from a node. */
  calls(nodeAddress: string, callDefinitions: CallConfig[]): this {
    for (const call of callDefinitions) {
      const edge: Edge = {
        from: nodeAddress,
        to: call.to,
        rate: call.rate,
        type: call.type ?? "invoke",
      };
      if (call.dataSize) {
        edge.dataSize = call.dataSize;
      }
      this._edges.push(edge);
    }
    return this;
  }

  /** Set usage metrics for a node. */
  usage(nodeAddress: string, usage: NodeUsage): this {
    if (!this._nodes[nodeAddress]) {
      this._nodes[nodeAddress] = {};
    }
    (this._nodes[nodeAddress] as Record<string, unknown>).usageMetrics =
      usage.metrics;
    return this;
  }

  /** Set pricing rates for a node. */
  pricingRates(nodeAddress: string, rates: Record<string, number>): this {
    if (!this._nodes[nodeAddress]) {
      this._nodes[nodeAddress] = {};
    }
    (this._nodes[nodeAddress] as Record<string, unknown>).pricingRates = rates;
    return this;
  }

  /** Set flat override mode (escape hatch per Principle 9). */
  setFlatOverride(nodeAddress: string, value: boolean = true): this {
    if (!this._nodes[nodeAddress]) {
      this._nodes[nodeAddress] = {};
    }
    (this._nodes[nodeAddress] as Record<string, unknown>).flatOverride = value;
    return this;
  }

  /** Get the name. */
  get name(): string {
    return this._name;
  }

  /** Get the edges array. */
  get edges(): Edge[] {
    return [...this._edges];
  }

  /** Get the nodes record. */
  get nodes(): Record<string, Record<string, unknown>> {
    return { ...this._nodes };
  }

  /**
   * Export to the canonical CostModel representation.
   *
   * This is the cross-surface interchange format — the same structure
   * produced by YAML and Python SDKs.
   */
  toCostModel(): CostModel {
    if (!this._entry) {
      throw new Error("Workflow entry node not set. Call setEntry().");
    }
    if (!this._frequency) {
      throw new Error("Workflow frequency not set. Call setFrequency().");
    }

    const model: CostModel = {
      version: "1.0",
      workflow: {
        name: this._name,
        entry: this._entry,
        frequency: this._frequency,
        ...(Object.keys(this._parameters).length > 0
          ? { parameters: this._parameters }
          : {}),
      },
      nodes: this._nodes as unknown as Record<string, CostModel["nodes"][string]>,
      edges: this._edges.length > 0 ? this._edges : undefined,
    };

    return model;
  }

  /**
   * Serialize to JSON string matching cost-model.schema.json.
   */
  toJSON(): string {
    return JSON.stringify(this.toCostModel(), null, 2);
  }
}
