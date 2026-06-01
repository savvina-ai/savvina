# Charts and BI

Savvina AI turns every query result into a configurable chart. Charts appear directly beneath the results table and are auto-suggested based on the shape of the data — no extra steps required.

---

## How Charts Are Suggested Automatically

When a query returns results, Savvina inspects the column types and applies these rules (first match wins):

| Data shape | Default chart |
|---|---|
| One row, one numeric column whose name contains _rate_, _pct_, _percent_, _utilisation_, or _score_ | **Gauge** |
| One row, one numeric column | **Number card** (KPI) |
| Date/timestamp column + numeric column(s) | **Line** (time series) |
| Non-date label column + numeric column(s) | **Bar** |
| Anything else | **Bar** (first two columns) |

Pie is intentionally never auto-suggested because bar is a safer default for all categorical data. You can always switch via the chart-type picker.

---

## Chart Types

| Type | When to use |
|---|---|
| **Bar** | Compare categories or discrete groups |
| **Line** | Show trends over time or ordered sequences |
| **Area** | Same as line but emphasises volume; stackable |
| **Pie** | Part-to-whole proportions for a small number of categories |
| **Scatter** | Correlation between two numeric columns |
| **Combo** | Mix bar and line series on the same axis (e.g. volume + rate) |
| **Number** | Single KPI metric with optional comparison value and trend arrow |
| **Gauge** | Single metric shown as a radial arc with configurable min/max |

---

## Using the Chart Editor

Click the chart icon or the **Edit chart** button beneath any result to open the editor panel. Changes apply instantly.

### Chart type

Click any of the eight icons across the top of the editor to switch chart type. Incompatible options (e.g. stacking on a scatter chart) are automatically cleared.

### Title

Free-text label shown centred above the chart.

### X-axis and Y-axis

- **X-axis** — select any column as the category or time dimension.
- **Y-axis** — add one or more numeric columns as series. Click the **+ Add column** pill to add a series; click the **×** on a badge to remove one.
- For **Combo** charts, each Y-axis series has a small toggle (▌ / ∿) to switch it individually between bar and line.

### Group by and Aggregation

Available for bar, line, area, and combo charts.

- **Group by** — select a column whose unique values become separate series (pivot). Setting a group-by column automatically enables aggregation (defaults to **Sum**).
- **Aggregation** — how to combine rows that share the same X-value and series value: Sum, Count, Average, Min, or Max.

Example: X = `month`, Group by = `region`, Aggregation = `Sum` → one line per region, revenue summed per month.

### Options row

| Option | Effect |
|---|---|
| **Stacked** | Stack series on top of each other (bar, area, combo-bar) |
| **Data labels** | Show values directly on bars or line points |
| **Exclude nulls** | Drop rows where the X-axis value is null before rendering |
| **Connect nulls** | Join line/area series across null gaps instead of breaking |
| **Show legend** | Toggle the series legend on/off |
| **Position** | Place the legend at top, bottom, left, or right |

### Trend Line

Available for bar, line, area, and combo charts. Expand the **Trend line** section:

- **Linear regression** — least-squares fit over the row index; shows the overall slope of the data.
- **Moving average** — causal (trailing) average; configure the window size (2–20 rows).

The trend line is rendered as a dashed overlay and does not appear in the legend.

### Filters

Expand the **Filters** section to add one or more client-side row filters that apply before aggregation. Each filter has:

- **Column** — any column returned by the query
- **Operator** — `=`, `≠`, `>`, `≥`, `<`, `≤`, or `contains`
- **Value** — the comparison string or number

Multiple filters are ANDed together.

### Axis Options

Expand **Axis options** for fine-grained control:

| Control | Description |
|---|---|
| **Y scale** | Linear (default) or Logarithmic |
| **Y range** | Override the auto min/max for the Y axis |
| **X labels** | Rotate X-axis tick labels: horizontal, −45°, or −90° (useful for long category names) |

---

## Number / KPI Cards

When the chart type is **Number**, the result is displayed as a large headline figure.

| Setting | Description |
|---|---|
| **Value column** | The single numeric column to display |
| **Format** | Integer, Decimal, Currency, or Percent |
| **Currency symbol** | Custom symbol when format is Currency (default `$`) |
| **Vs.** | Optional comparison value — shows a delta and an up/down/flat arrow |
| **Label** | Description of the comparison (e.g. `vs last month`) |

---

## Gauge Charts

When the chart type is **Gauge**, the result is rendered as a semicircular radial arc.

| Setting | Description |
|---|---|
| **Value column** | The single numeric column to display |
| **Range** | Configurable min and max for the gauge scale |
| **Format** | Integer, Decimal, Currency, or Percent |

Colour thresholds are defined in the chart config (`gaugeThresholds`) — each threshold sets the fill colour for values at or above its level.

---

## Exporting Results

### Client-side exports (instant, no server round-trip)

Available via the **Export** menu on any executed result:

| Format | Behaviour |
|---|---|
| **CSV** | Downloads `query-results.csv` — headers + all rows, RFC 4180 compliant |
| **JSON** | Downloads `query-results.json` — array of row objects, pretty-printed |
| **PNG** | Downloads the rendered chart as a PNG image (`chart-<id>.png`) — available when a chart is displayed |

### Server-side exports

| Format | How to trigger | Notes |
|---|---|---|
| **XLSX** | Export → **Excel** | Server renders the spreadsheet; downloads as `query-<id>.xlsx` |
| **CSV (backend)** | Export → **CSV (server)** | Useful when the full result set was truncated in the UI |

### PDF Reports

Combine multiple query results — including embedded chart images — into a single PDF:

1. Open the **Report** panel and give the report a title.
2. Add sections: each section can reference a message ID and optionally include a chart screenshot and heading.
3. Click **Download PDF**. The file is generated server-side and downloaded as `<title>.pdf`.

---

## Tips

- **Time series:** Ask for results with a date column first, e.g. `Show monthly revenue for 2024`. The line chart will be auto-suggested.
- **Stacked bars:** Ask for sales by product and region, then set Group by = `region`, Aggregation = `Sum`, and check **Stacked**.
- **KPI dashboard feel:** Ask a single-value question like `What is the total revenue this quarter?` and the result auto-renders as a Number card.
- **Logarithmic scale:** Use Y scale → Logarithmic when comparing values that span several orders of magnitude (e.g. page views by country).
- **Long category labels:** Rotate X labels to −45° or −90° when bar chart labels overlap.
