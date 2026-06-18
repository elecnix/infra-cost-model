/**
 * TypeScript type definitions for the infrastructure cost model representation.
 *
 * Generated from cost-model.schema.json — the single source of truth for
 * YAML, TypeScript, and Python interfaces (Principle 11).
 */

// ── Node Types ───────────────────────────────────────────────────────────────

/** Valid node types in a DAG cost model. */
export type NodeType = "compute" | "storage" | "routing" | "external";

/** Supported cloud providers. */
export type Provider = "aws" | "azure" | "gcp" | "bedrock" | "openai";

/** Pricing model types. */
export type PricingModel = "tiered" | "flat" | "token_based" | "percentage";

/** Edge call types. */
export type EdgeType = "read" | "write" | "invoke";

/** Frequency time units for workflow entry rate. */
export type FrequencyUnit = "perSecond" | "perMinute" | "perHour" | "perDay";

// ── Usage Metrics ────────────────────────────────────────────────────────────

/** A single usage metric definition. */
export interface UsageMetric {
  unit: string;
  description?: string;
  /** Per-invocation multiplier. When flatOverride is true, this is used
   * directly as a flat monthly total instead. */
  value?: number;
}

// ── Pricing ──────────────────────────────────────────────────────────────────

/** A single tier in tiered pricing. */
export interface TieredPrice {
  start: number;
  end: number | null;
  price: number;
}

// ── Data Size ────────────────────────────────────────────────────────────────

/** Data size specification for an edge. */
export interface DataSize {
  unit: string;
  average?: number;
  minimum?: number;
  maximum?: number;
}

// ── Nodes and Edges ──────────────────────────────────────────────────────────

/** A node in the cost model DAG. */
export interface CostNode {
  nodeType: NodeType;
  resourceAddress: string;
  provider?: Provider;
  service?: string;
  region?: string;
  usageMetrics?: Record<string, UsageMetric>;
  pricingModel?: PricingModel;
  pricingRates?: Record<string, number>;
  /** Escape hatch: when true, usageMetrics values are flat monthly totals
   * instead of per-invocation multipliers (Principle 9). */
  flatOverride?: boolean;
}

/** An edge in the cost model DAG. */
export interface Edge {
  from: string;
  to: string;
  rate: number;
  type?: EdgeType;
  dataSize?: DataSize;
}

// ── Workflow ─────────────────────────────────────────────────────────────────

/** Workflow entry frequency. */
export interface Frequency {
  unit: FrequencyUnit;
  value: number;
}

/** Workflow definition — the root of a cost model. */
export interface WorkflowDef {
  name: string;
  entry: string;
  frequency: Frequency;
  parameters?: Record<string, number>;
}

// ── Cost Model Representation ────────────────────────────────────────────────

/**
 * The canonical Cost Model Representation.
 *
 * This is the output of all three SDK surfaces (YAML, TypeScript, Python).
 * It can be serialized to JSON and consumed by the cost engine, validated
 * against cost-model.schema.json, or round-tripped through YAML.
 */
export interface CostModel {
  version: "1.0";
  workflow: WorkflowDef;
  nodes: Record<string, CostNode>;
  edges?: Edge[];
}

// ── Engine Input ─────────────────────────────────────────────────────────────

/** Usage parameters for a resource address. */
export interface UsageParams {
  resourceAddress: string;
  usageMetrics: Record<string, number>;
}

// ── Engine Output ────────────────────────────────────────────────────────────

/** Derived usage for a single node. */
export interface DerivedUsage {
  resourceAddress: string;
  invocationCount: number;
  /** Per-metric consumption totals. */
  consumption: Record<string, number>;
  /** Flag indicating this is a flat override (escape hatch). */
  flatOverride: boolean;
}

/** Cost breakdown for a single node. */
export interface NodeCost {
  address: string;
  nodeType: NodeType;
  cost: number;
  details: Record<string, number>;
}

/** Full cost model output. */
export interface CostOutput {
  workflow: string;
  totalCost: number;
  nodeCosts: NodeCost[];
  derivedUsage: Record<string, DerivedUsage>;
  timeBasis: FrequencyUnit;
}

// ── SDK Builder Types ────────────────────────────────────────────────────────

/** Configuration for a single call edge. */
export interface CallConfig {
  to: string;
  rate: number;
  type?: EdgeType;
  dataSize?: DataSize;
}

/** A usage metric value with optional unit. */
export interface MetricValue {
  value: number;
  unit?: string;
}
