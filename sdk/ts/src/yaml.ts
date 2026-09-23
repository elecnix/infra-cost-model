/**
 * YAML DSL parser — TypeScript equivalent of parse_yaml_dsl() in the Python SDK.
 *
 * Parses the arrow-syntax DSL format from DESIGN_PRINCIPLES.md into the
 * canonical CostModel representation.
 */

import * as yaml from "js-yaml";
import type { CostModel, Edge, FrequencyUnit } from "./types";

const UNIT_MAP: Record<string, FrequencyUnit> = {
  sec: "perSecond",
  min: "perMinute",
  hr: "perHour",
  day: "perDay",
  week: "perWeek",
  month: "perMonth",
};

/** The key that pins the engine versions a model needs (see version_requirement.py). */
const ENGINE_REQUIREMENT_KEY = "requiresEngine";

/**
 * Parse a YAML DSL string into a CostModel representation.
 *
 * Accepts one `workflow` or a `workflows` array of independent workflows
 * that share the nodes, as the schema does. Supports the arrow syntax
 * format, with the Unicode arrow or the ASCII `->`:
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

  let workflows: Record<string, unknown>[];
  if ("workflow" in data) {
    workflows = [data.workflow as Record<string, unknown>];
  } else if (Array.isArray(data.workflows) && data.workflows.length > 0) {
    workflows = data.workflows as Record<string, unknown>[];
  } else {
    throw new Error("YAML must have a 'workflow' or 'workflows' section");
  }

  // Handle shorthand frequency notation (e.g., "1000/min")
  for (const workflow of workflows) {
    const freq = workflow.frequency;
    if (typeof freq === "string" && freq.includes("/")) {
      const [value, unit] = freq.split("/");
      workflow.frequency = {
        value: parseFloat(value!),
        unit: UNIT_MAP[unit!] ?? "perMinute",
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
        // Arrow syntax: "→ aws_lambda_function.foo: 1" or "-> aws_lambda_function.foo: 1"
        let targetAddr: string;
        if (key.startsWith("\u2192 ")) {
          targetAddr = key.slice(2);
        } else if (key.startsWith("-> ")) {
          targetAddr = key.slice(3);
        } else {
          continue;
        }
        if (typeof value === "number") {
          edges.push({ from: sourceAddr, to: targetAddr, rate: value });
        } else if (typeof value === "object" && value !== null) {
          const v = value as Record<string, unknown>;
          const edge: Edge = {
            from: sourceAddr,
            to: targetAddr,
            rate: (v.rate as number) ?? 1.0,
          };
          if ("type" in v) edge.type = v.type as Edge["type"];
          if ("dataSize" in v || "data_size" in v) {
            edge.dataSize = (v.dataSize ?? v.data_size) as Edge["dataSize"];
          }
          edges.push(edge);
        }
      }
    }
  }

  const model: Record<string, unknown> = { version: "1.0" };
  if ("workflow" in data) {
    model.workflow = workflows[0];
  } else {
    model.workflows = workflows;
  }
  model.nodes = nodes;
  model.edges = edges;

  // Carry the model's engine requirement across. This function rebuilds the
  // model from the fields it knows, so a key it does not copy would be
  // dropped before anything can check it.
  const requirement = data[ENGINE_REQUIREMENT_KEY];
  if (requirement !== undefined && requirement !== null) {
    model[ENGINE_REQUIREMENT_KEY] = requirement;
  }

  return model as unknown as CostModel;
}
