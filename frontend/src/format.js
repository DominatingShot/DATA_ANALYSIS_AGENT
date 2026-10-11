const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 });
const inrMoney = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 2 });

// Column names that hold rupee amounts, and ones that look like money but are not.
const MONEY = /(revenue|amount|price|sales|value|spend|bill_total|avg_bill|total)/i;
const NOT_MONEY = /(pct|percent|share|ratio|count|qty|quantity|units|lines|bills$|^bills|number|_no$|days|hour|rank|serving)/i;

export function isMoneyColumn(name) {
  return MONEY.test(name) && !NOT_MONEY.test(name);
}

export function formatCell(value, column) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (isMoneyColumn(column)) return "₹" + inrMoney.format(value);
    if (/(pct|percent|share)/i.test(column)) return inr.format(value) + "%";
    return inr.format(value);
  }
  return String(value);
}

export function humanizeColumn(name) {
  return name.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}
