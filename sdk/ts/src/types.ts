/**
 * TypeScript type definitions for the infrastructure cost model representation.
 *
 * Schema-derived types are generated from cost-model.schema.json — the single
 * source of truth for YAML, TypeScript, and Python interfaces (Principle 11).
 * Engine-level types (UsageParams, DerivedUsage, NodeCost, CostOutput) are
 * hand-maintained here since the schema defines only the representation layer.
 */

import type {
  InfrastructureCostModelRepresentation,
  Node,
  UsageMetric,
  Edge,
  NodeType,
} from "./types.generated";

// ── Re-exports from generated schema types ───────────────────────────────────

export type { NodeType, UsageMetric, Edge };

/** Canonical Cost Model Representation (alias for the schema root type). */
export type CostModel = InfrastructureCostModelRepresentation;

/** A node in the cost model DAG (alias for the schema Node type). */
export type CostNode = Node;

// ── Inline types extracted from schema (not standalone in generated output) ──

/** Supported cloud providers. */
export type Provider = "aws" | "azure" | "gcp" | "bedrock" | "openai";

/** Pricing model types. */
export type PricingModel = "tiered" | "flat" | "token_based" | "percentage";

/** Edge call types. */
export type EdgeType = "read" | "write" | "invoke";

/** Frequency time units for workflow entry rate. */
export type FrequencyUnit =
  | "perSecond"
  | "perMinute"
  | "perHour"
  | "perDay"
  | "perWeek"
  | "perMonth";

// ── Convenience aliases for schema-inlined structures ────────────────────────

/** Data size specification for an edge. */
export interface DataSize {
  unit?: string;
  average?: number;
  minimum?: number;
  maximum?: number;
  [k: string]: unknown;
}

/** Workflow entry frequency. */
export interface Frequency {
  unit: FrequencyUnit;
  value: number;
  [k: string]: unknown;
}

/** Workflow definition. */
export interface WorkflowDef {
  name: string;
  entry: string;
  frequency: Frequency;
  parameters?: Record<string, number>;
}

/** A single tier in tiered pricing. */
export interface TieredPrice {
  start: number;
  end: number | null;
  price: number;
}

// ── SDK Builder Types (not in schema) ───────────────────────────────────────

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

// ── Engine Input (not in schema) ────────────────────────────────────────────

/** Usage parameters for a resource address. */
export interface UsageParams {
  resourceAddress: string;
  usageMetrics: Record<string, number>;
}

// ── Engine Output (not in schema) ───────────────────────────────────────────

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
