// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

/**Size the rotated X-axis of a chart from its real labels, so none are clipped or overlap the legend. */

/** Upper bound for the X-axis band; longer labels are truncated with an ellipsis. */
export const MAX_X_AXIS_HEIGHT = 220;

/** Tick line + tick margin + the `dy` offset applied to rotated ticks + breathing room. */
const AXIS_CHROME = 20;
const ELLIPSIS = '…';
const FONT_FAMILY = 'ui-sans-serif, system-ui, sans-serif';
/** Average glyph width relative to the font size, used when no canvas is available. */
const FALLBACK_CHAR_WIDTH = 0.58;

let measureCtx: CanvasRenderingContext2D | null | undefined;

function getMeasureContext(): CanvasRenderingContext2D | null {
  if (measureCtx !== undefined) return measureCtx;
  try {
    measureCtx =
      typeof document === 'undefined' ? null : document.createElement('canvas').getContext('2d');
  } catch {
    measureCtx = null;
  }
  return measureCtx;
}

export function estimateTextWidth(text: string, fontSize: number): number {
  const ctx = getMeasureContext();
  if (ctx) {
    ctx.font = `${fontSize}px ${FONT_FAMILY}`;
    return ctx.measureText(text).width;
  }
  return text.length * fontSize * FALLBACK_CHAR_WIDTH;
}

export function truncateToWidth(text: string, maxWidth: number, fontSize: number): string {
  if (estimateTextWidth(text, fontSize) <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (estimateTextWidth(text.slice(0, mid) + ELLIPSIS, fontSize) <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return text.slice(0, lo).trimEnd() + ELLIPSIS;
}

export interface XAxisLayout {
  /** Axis band height; undefined lets Recharts use its default. */
  height: number | undefined;
  /** Widest a label may be before it must be truncated. */
  maxLabelWidth: number;
  /** How far the first rotated label extends left of its tick. */
  leftOverflow: number;
}

export function computeXAxisLayout(
  labels: string[],
  angle: number,
  fontSize: number,
): XAxisLayout {
  if (angle === 0) return { height: undefined, maxLabelWidth: Infinity, leftOverflow: 0 };

  const rad = (Math.abs(angle) * Math.PI) / 180;
  const sin = Math.sin(rad);
  const cos = Math.cos(rad);
  // Vertical extent of a label of width w: w·sin + fontSize·cos.
  const maxLabelWidth = Math.floor((MAX_X_AXIS_HEIGHT - AXIS_CHROME - fontSize * cos) / sin);

  const longest = labels.reduce((m, l) => Math.max(m, estimateTextWidth(l, fontSize)), 0);
  const width = Math.min(longest, maxLabelWidth);
  const height = Math.min(
    MAX_X_AXIS_HEIGHT,
    Math.ceil(width * sin + fontSize * cos) + AXIS_CHROME,
  );
  // cos is ~6e-17 at 90°, so round it away instead of reporting a phantom overflow.
  const leftOverflow = Math.max(0, Math.ceil(width * cos - 1e-9));

  return { height, maxLabelWidth, leftOverflow };
}
