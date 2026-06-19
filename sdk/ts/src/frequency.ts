/**
 * Frequency constructors — mirroring the Python SDK's per_second(), per_minute(), etc.
 */

import type { Frequency, FrequencyUnit } from "./types";

/** Create a per-second frequency. */
export function perSecond(value: number): Frequency {
  return { value, unit: "perSecond" };
}

/** Create a per-minute frequency. */
export function perMinute(value: number): Frequency {
  return { value, unit: "perMinute" };
}

/** Create a per-hour frequency. */
export function perHour(value: number): Frequency {
  return { value, unit: "perHour" };
}

/** Create a per-day frequency. */
export function perDay(value: number): Frequency {
  return { value, unit: "perDay" };
}

/** Create a per-week frequency. */
export function perWeek(value: number): Frequency {
  return { value, unit: "perWeek" };
}

/** Create a per-month frequency. */
export function perMonth(value: number): Frequency {
  return { value, unit: "perMonth" };
}

/** Parse a shorthand frequency string like "1000/min". */
export function parseFrequency(shorthand: string): Frequency {
  const match = shorthand.match(/^([\d.]+)\/(sec|min|hr|day|week|month)$/);
  if (!match) {
    throw new Error(
      `Invalid frequency shorthand: "${shorthand}". Expected format: "1000/min".`
    );
  }
  const value = parseFloat(match[1]!);
  const unitMap: Record<string, FrequencyUnit> = {
    sec: "perSecond",
    min: "perMinute",
    hr: "perHour",
    day: "perDay",
    week: "perWeek",
    month: "perMonth",
  };
  return { value, unit: unitMap[match[2]!]! };
}
