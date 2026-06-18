/**
 * YAML DSL parser — TypeScript equivalent of parse_yaml_dsl() in the Python SDK.
 *
 * Parses the arrow-syntax DSL format from DESIGN_PRINCIPLES.md into the
 * canonical CostModel representation.
 */

import * as yaml from "js-yaml";
import type { CostModel, Edge, FrequencyUnit } from "./types";

/**
 * Parse a YAML DSL string into a CostModel representation.
 *
 * Supports the arrow syntax format:
 *
 * ```yaml
 * calls:
 *   aws_api_gatewayv2_api.llm_api:
 *     data_out: 50KB
 *     → aws_lambda_function.orchestrator: 1
 * ```
 */
export function parseYamlDsl(yamlContent: string): CostModel {
  const data = yaml.load(yamlContent) as Record<string, unknown>;

  if (!data.workflow || typeof data.workflow !== "object") {
    throw new Error("YAML must have 'workflow' section");
  }

  const workflow = data.workflow as Record<string, unknown>;

  // Handle shorthand frequency notation (e.g., "1000/min")
  let freq = workflow.frequency;
  if (typeof freq === "string") {
    const parts = freq.split("/");
    if (parts.length === 2) {
      const unitMap: Record<string, FrequencyUnit> = {
        sec: "perSecond",
        min: "perMinute",
        hr: "perHour",
        day: "perDay",
      };
      workflow.frequency = {
        value: parseFloat(parts[0]!),
        unit: unitMap[parts[1]!] ?? "perMinute",
      };
    }
  }

  // Extract edges and nodes
  const edges: Edge[] = (data.edges as Edge[]) ?? [];
  const nodes = (data.nodes ?? {}) as Record<string, Record<string, unknown>>;
  const calls = data.calls as Record<string, Record<string, unknown>> | undefined;

  // Parse calls section with arrow syntax (DSL format)
  if (calls) {
    for (const [sourceAddr, callDefs] of Object.entries(calls)) {
      if (typeof callDefs !== "object" || callDefs === null) continue;

      for (const [key, value] of Object.entries(callDefs)) {
        // Arrow syntax: "→ aws_lambda_function.foo: 1"
        if (key.startsWith("\u2192 ") || key.startsWith("→ ")) {
          const targetAddr = key.slice(2);
          if (typeof value === "number") {
            edges.push({ from: sourceAddr, to: targetAddr, rate: value });
          } else if (typeof value === "object" && value !== null) {
            const v = value as Record<string, unknown>;
            edges.push({
              from: sourceAddr,
              to: targetAddr,
              rate: (v.rate as number) ?? 1.0,
              type: v.type as Edge["type"],
              dataSize: (v.dataSize ?? v.data_size) as Edge["dataSize"],
            });
          }
        } else if (key === "data_out") {
          if (!nodes[sourceAddr]) {
            nodes[sourceAddr] = {};
          }
          nodes[sourceAddr]!.dataOut = value;
        }
      }
    }
  }

  return {
    version: "1.0",
    workflow: workflow as unknown as CostModel["workflow"],
    nodes: nodes as unknown as CostModel["nodes"],
    edges,
  };
}
