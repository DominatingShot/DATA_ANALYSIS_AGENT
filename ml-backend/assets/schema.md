# Restaurant Analytics Schema

Data model backing a natural-language analytics chatbot for a restaurant. Two tables: a slowly-changing menu dimension and a sales fact table.

**Platform:** PostgreSQL 16
**Currency:** INR (₹)
**Source data:** Alankar Signature, East Fort / Swaraj Round, Thrissur — multi-cuisine family restaurant, no alcohol.
**Purpose:** answer questions about sales volume, revenue, trading patterns and price-change scenarios, and produce charts from the results.

---

## How to use this document

This file is the complete context for two agents:

- **Plausibility check** — decide whether a user's question can be answered from this data at all. Read §1 and §2; that is usually sufficient.
- **SQL generation** — write a correct query. Read §2 before writing anything, then §4–§7 for the specifics.

Both tables live in the default schema (`public`) and are referenced unqualified: `dim_menu`, not `dbo.dim_menu`. There is no `dbo` schema. No views currently exist; queries go against the base tables.

---

## 1. Scope

Everything answerable is derivable from the columns documented in §5 and §6. If a question needs a fact that is not in those columns, it cannot be answered from this data — say so and name what is missing. Do not estimate, allocate or substitute a proxy.

Interpret questions generously before rejecting them. Loose business wording usually maps onto a column that does exist: "most profitable dish" almost always means the dish that brings in the most money, which is `SUM(gross_amount)` by item. Answer it, and **label the figure for what it actually is — revenue**. Only if the user then asks about profit after costs does the absence of cost data become the answer.

### Two columns that are narrower than they sound

| Column | Is not |
|---|---|
| `gross_amount` | Net of anything. There is no cost, tax or service-charge column, so every money figure is gross revenue and must be described that way |
| `bill_number` | A customer. It identifies one bill. The same person on two visits is two unrelated bills |

### Coverage window

Data runs **2024-10-01 to 2026-09-30**. A query whose date range falls wholly or partly outside that window will return no rows or an understated total. Do not report that as a decline in trade — state that the period is outside the loaded data.

---

## 2. Critical rules

These are the errors that produce **wrong numbers without raising an error**. A chart built on any of them looks entirely plausible. Read this section before writing any query.

### 2.1 Always join on `menu_sk`. Never join on `item_name`.

```sql
-- CORRECT
FROM fact_sales f
JOIN dim_menu  m ON m.menu_sk = f.menu_sk
```

```sql
-- WRONG — inflates every total
FROM fact_sales f
JOIN dim_menu  m ON m.item_name = f.item_name
```

`dim_menu` holds one row per **version** of an item, not one row per item. Joining on `item_name` matches every historical version of the dish, so an item with four price periods has each of its sales counted four times. `menu_sk` already identifies exactly one version, so the correct join needs no date logic.

When reporting by dish, **group by `item_name`, not `menu_sk`** — otherwise one dish appears as four separate rows, one per price period.

### 2.2 Version date ranges are inclusive at both ends

A version is in force on date `d` when:

```sql
d BETWEEN effective_from AND effective_to
```

The half-open form `d >= effective_from AND d < effective_to` does **not** describe this data. Applied to the loaded facts it drops 395 sale lines — every sale made on the final day of a price period — silently.

Note the asymmetry, and do not let it bleed across: **fact dates (`sale_date`) are filtered half-open; dimension version ranges are matched with `BETWEEN`.** The conventions differ because `effective_to` is a closed endpoint and a month boundary is not.

### 2.3 `is_current` is for today's menu only

Filter `dim_menu` on `is_current` to list what is on the menu now. Do **not** apply it when aggregating historical sales — it drops every sale made under an older version. Across the four price waves in the loaded period, that discards most of the history.

Discontinued items have no current row at all, so `is_current` cannot be used to enumerate every dish ever sold. See §5.3.

### 2.4 `unit_price` is non-additive

`SUM(unit_price)` is never meaningful. Use `SUM(gross_amount)` for revenue. Average price must be weighted by quantity:

```sql
SUM(gross_amount) / SUM(quantity)
```

The additive facts are `quantity` and `gross_amount`.

### 2.5 Bill-level metrics need a subquery

There is no bill header table. Aggregate to `bill_number` first, then average. Averaging `gross_amount` directly gives the average *line* value, a different and usually unintended number. See §6.3.

### 2.6 `meal_type` is not a food-type split

Parottas, rice, soups and drinks are all `veg`, so a `meal_type` breakdown puts revenue near 50/50 and badly misleads anyone asking "how much of our business is vegetarian". For that question filter on `category` instead — `veg` against the protein categories — and **say which definition is being used**.

---

## 3. Design decisions

The reasoning behind three choices that constrain what can be asked.

### Why there is no cost column

Restaurants buy ingredients for the whole kitchen, not per dish, so per-dish cost can only ever be an allocation — an estimate dressed up as a fact. It is deliberately excluded.

**Consequence:** every money figure this schema produces is **revenue**, and must be labelled as revenue rather than earnings or profit. Questions that genuinely need cost — margin percentage, whether an item sells below cost — have no answer here.

This does **not** block the main price-scenario question. The extra profit from raising a price equals the extra revenue, because kitchen costs do not change when the menu price changes. See §6.1.

If true profit is needed later, the conventional approach is a separate monthly expense table (rent, salaries, total ingredient purchases, utilities) giving profit at month grain. That is a new table and does not alter these two.

### Why `sale_date` and `sale_time` are separate columns

`sale_date` is a `DATE`, which in PostgreSQL stores no time at all — writing a timestamp into it truncates the time silently, with no error. Keeping time in its own column preserves `sale_date` as the natural key to a future `dim_date` and keeps the grain clean, which is the standard dimensional treatment. A single `TIMESTAMP` would have muddled both.

### Why `unit_price` sits on the fact even though `dim_menu.item_price` exists

`unit_price` records what was actually charged, which can differ from the listed price (staff discount, combo rate). `dim_menu.item_price` is an **attribute** of the item; `fact_sales.unit_price` is a **measurement** of a transaction.

---

## 4. Data model

| Table | Grain (what one row means) | Rows per item |
|---|---|---|
| `dim_menu` | One **version** of one menu item | Many — a new row each time the item changes |
| `fact_sales` | One **dish line** on one bill | One per line item sold |

The two are joined on `menu_sk`.

```
dim_menu (menu_sk)  ──1───────M──  fact_sales (menu_sk)
```

A single real-world dish like "Beef Fry" may occupy several rows in `dim_menu` — one per price period. Each sale points at the specific version that was in force on the day it was sold.

---

## 5. `dim_menu`

Menu catalogue with full history. Implemented as a **Slowly Changing Dimension, Type 2**: attributes are never overwritten; a change closes the existing row and opens a new one.

### 5.1 Columns

| Column | Type | Null | Description |
|---|---|---|---|
| `menu_sk` | INTEGER IDENTITY | No | **Surrogate key.** Identifies one *version* of an item, not the item itself. Primary key |
| `item_name` | VARCHAR(100) | No | Business key. Stable across versions — this is what identifies the dish over time |
| `description` | VARCHAR(500) | Yes | Free-text menu description |
| `category` | VARCHAR(30) | No | One of 34 menu sections. See below |
| `meal_type` | VARCHAR(7) | No | `veg` or `non veg`. See §2.6 before using this for a veg/non-veg split |
| `serving_size` | NUMERIC(2,1) | No | Estimated number of people a normal portion serves. 1.0 to 4.0 in steps of 0.5. Portions, not grams — a planning hint, not a measured fact |
| `item_price` | NUMERIC(10,2) | No | Listed menu price for this version |
| `effective_from` | DATE | No | First date this version applied. **Inclusive** |
| `effective_to` | DATE | No | Last date this version applied. **Inclusive.** Open rows use `9999-12-31` |
| `is_current` | BOOLEAN | No | `TRUE` for the version in force today. Convenience flag — derivable from the dates. See §2.3 |

### 5.2 Categories

The category list is not a generic four-way split. It mirrors the restaurant's own menu sections, which is what makes "revenue by category" a question the owner recognises.

| Group | Values |
|---|---|
| Mains by protein | `beef`, `chicken`, `egg`, `fish`, `mutton`, `prawns`, `veg` |
| Rice and noodles | `biryani`, `fried rice`, `kizhi parotta`, `kothu parotta`, `noodles` |
| Breads | `breads` |
| Grilled and Arabic | `arabic`, `arabic grilled fish`, `grills`, `kebab`, `tandoori`, `tawa chicken`, `tawa fish`, `tikka` |
| Starters and sides | `continental`, `platter`, `salad`, `sides`, `soup`, `starters` |
| Drinks | `fresh juice`, `hot drink`, `ice cream shake`, `lime`, `mojito` |
| Sweets | `dessert`, `falooda` |

Two of the 34 — `arabic grilled fish` and `tawa fish` — are permitted by the constraint but have **no rows**. Those dishes are priced "Ask for price" on the printed menu, and `item_price` is `NOT NULL`, so they could not be loaded. A `GROUP BY category` will not return them.

### 5.3 Discontinued items have no current row

Five items were taken off the menu during the loaded period. Their last version has `is_current = FALSE` and a real `effective_to`, and **no successor row exists**. So `WHERE is_current` returns 286 rows, not 291. That is correct for "show me today's menu", but it cannot enumerate every dish the restaurant has ever sold.

```sql
-- Every dish ever sold, with its latest known price
SELECT DISTINCT ON (item_name) item_name, item_price, effective_from, effective_to
FROM   dim_menu
ORDER  BY item_name, effective_from DESC;
```

### 5.4 Portion sizes live in `item_name`

Three dishes are sold in portions, each at its own price. The table has one `item_price` per row, so each portion is a separate item, distinguished by a suffix on the name:

```
Tandoori Chicken - Quarter / - Half / - Full
Alankar Signature Al-Faham - Quarter / - Half / - Full
Chicken Bamboo Stick Mandi - Quarter / - Half / - Full
```

`serving_size` for these is 1.0, 2.0 and 4.0.

### 5.5 Constraints

| Name | Rule |
|---|---|
| `pk_dim_menu` | `menu_sk` |
| `ck_menu_meal_type` | `meal_type IN ('veg','non veg')` |
| `ck_menu_category` | `category` is one of the 34 values above |
| `ck_menu_serving` | Between 1 and 4, and a multiple of 0.5 |
| `ck_menu_price` | `item_price >= 0` |
| `ck_menu_dates` | `effective_to > effective_from` |

Constraint names are lowercase. PostgreSQL folds unquoted identifiers to lowercase, so `PK_dim_menu` in a script becomes `pk_dim_menu` in the catalogue.

Note that `ck_menu_dates` constrains each row in isolation; it does not prevent two versions of the same item from overlapping. That guarantee comes from the load process, not the database. It has been verified across all 593 version boundaries in the loaded data: every one has a clean one-day gap between the old end date and the new start date, with no overlaps anywhere.

### 5.6 DDL

```sql
CREATE TABLE dim_menu (
    menu_sk        INTEGER GENERATED ALWAYS AS IDENTITY,
    item_name      VARCHAR(100) NOT NULL,
    description    VARCHAR(500),
    category       VARCHAR(30) NOT NULL,
    meal_type      VARCHAR(7) NOT NULL,
    serving_size   NUMERIC(2,1) NOT NULL,
    item_price     NUMERIC(10,2) NOT NULL,
    effective_from DATE NOT NULL,
    effective_to   DATE NOT NULL DEFAULT DATE '9999-12-31',
    is_current     BOOLEAN NOT NULL DEFAULT TRUE,

    CONSTRAINT pk_dim_menu PRIMARY KEY (menu_sk),
    CONSTRAINT ck_menu_meal_type CHECK (meal_type IN ('veg', 'non veg')),
    CONSTRAINT ck_menu_category CHECK (category IN (
        'arabic', 'arabic grilled fish', 'beef', 'biryani', 'breads', 'chicken',
        'continental', 'dessert', 'egg', 'falooda', 'fish', 'fresh juice',
        'fried rice', 'grills', 'hot drink', 'ice cream shake', 'kebab', 'kizhi parotta',
        'kothu parotta', 'lime', 'mojito', 'mutton', 'noodles', 'platter',
        'prawns', 'salad', 'sides', 'soup', 'starters', 'tandoori',
        'tawa chicken', 'tawa fish', 'tikka', 'veg'
    )),
    CONSTRAINT ck_menu_serving CHECK (serving_size BETWEEN 1 AND 4
                                  AND MOD(serving_size, 0.5) = 0),
    CONSTRAINT ck_menu_price CHECK (item_price >= 0),
    CONSTRAINT ck_menu_dates CHECK (effective_to > effective_from)
);

CREATE INDEX ix_dim_menu_name_dates ON dim_menu (item_name, effective_from, effective_to);
```

---

## 6. `fact_sales`

One row per dish line on a bill.

### 6.1 Columns

| Column | Type | Null | Description |
|---|---|---|---|
| `sales_sk` | BIGINT IDENTITY | No | Surrogate key. Primary key |
| `menu_sk` | INTEGER | No | FK to `dim_menu`. Points at the version in force on `sale_date` |
| `bill_number` | INTEGER | No | Bill this line belongs to. **Repeats** — one bill has many lines |
| `line_number` | SMALLINT | No | Position of this line on its bill. Starts at 1 |
| `table_no` | SMALLINT | Yes | Physical table, 1–26. **`NULL` means takeaway / parcel** |
| `sale_date` | DATE | No | Date of the transaction. Date only — no time component |
| `sale_time` | TIME | No | Time the bill was settled. All lines of one bill share it |
| `quantity` | SMALLINT | No | Plates of this dish on this line. Greater than 0 |
| `unit_price` | NUMERIC(10,2) | No | Price actually charged per plate. Non-additive — see §2.4 |
| `gross_amount` | NUMERIC(12,2) | No | `quantity × unit_price`. Revenue for this line |

### 6.2 Structural notes

**`bill_number` is a degenerate dimension.** It is an identifier with no table behind it. Bill-level attributes — `table_no`, `sale_date`, `sale_time` — are repeated on every line of the bill. This is intentional denormalization for an analytics workload: it removes a join from the majority of queries. The trade-off is that nothing in the database prevents two lines of one bill from carrying different `table_no` values; the load process is responsible for that consistency.

**`bill_number` is globally unique and runs in settlement order.** It does not reset daily. Within any one day, a higher `bill_number` always has a later or equal `sale_time`, as a real POS would produce.

**No bill crosses midnight.** Late trading is capped at 23:59 rather than rolling into the next calendar day, so `sale_date` and the business day are always the same thing.

**`uq_fact_sales_line`** is the idempotency guard. Re-running a load fails loudly instead of silently doubling revenue.

### 6.3 Constraints

| Name | Rule |
|---|---|
| `pk_fact_sales` | `sales_sk` |
| `uq_fact_sales_line` | `(bill_number, line_number)` unique |
| `fk_fact_sales_menu` | `menu_sk` references `dim_menu(menu_sk)` |
| `ck_sales_qty` | `quantity > 0` |
| `ck_sales_price` | `unit_price >= 0` |

### 6.4 DDL

```sql
CREATE TABLE fact_sales (
    sales_sk     BIGINT GENERATED ALWAYS AS IDENTITY,
    menu_sk      INTEGER NOT NULL,
    bill_number  INTEGER NOT NULL,
    line_number  SMALLINT NOT NULL,
    table_no     SMALLINT,
    sale_date    DATE NOT NULL,
    sale_time    TIME NOT NULL,
    quantity     SMALLINT NOT NULL DEFAULT 1,
    unit_price   NUMERIC(10,2) NOT NULL,
    gross_amount NUMERIC(12,2) NOT NULL,

    CONSTRAINT pk_fact_sales PRIMARY KEY (sales_sk),
    CONSTRAINT uq_fact_sales_line UNIQUE (bill_number, line_number),
    CONSTRAINT fk_fact_sales_menu FOREIGN KEY (menu_sk) REFERENCES dim_menu (menu_sk),
    CONSTRAINT ck_sales_qty CHECK (quantity > 0),
    CONSTRAINT ck_sales_price CHECK (unit_price >= 0)
);

CREATE INDEX ix_fact_sales_date ON fact_sales (sale_date);
CREATE INDEX ix_fact_sales_menu ON fact_sales (menu_sk);
```

---

## 7. SCD Type 2 mechanics

### 7.1 How a version chain looks

The old row's `effective_to` is the **day before** the new row's `effective_from`.

```
Beef Fry   240.00   2024-10-01 .. 2025-01-14
Beef Fry   250.00   2025-01-15 .. 2025-11-09
Beef Fry   270.00   2025-11-10 .. 2026-03-31
Beef Fry   290.00   2026-04-01 .. 9999-12-31   (is_current)
```

### 7.2 How a change is recorded

A change to any tracked attribute (price, description, category, serving size) closes the current row and opens a new one. Both operations run in a single transaction so the version ranges meet with no gap and no overlap.

```sql
BEGIN;

WITH closed AS (
    UPDATE dim_menu
       SET effective_to = DATE '2026-09-15' - 1,   -- inclusive end: the day before
           is_current   = FALSE
     WHERE item_name = 'Paneer Butter Masala'
       AND is_current
 RETURNING item_name, description, category, meal_type, serving_size
)
INSERT INTO dim_menu (item_name, description, category, meal_type,
                      serving_size, item_price, effective_from)
SELECT item_name, description, category, meal_type, serving_size,
       330.00, DATE '2026-09-15'
FROM   closed;

COMMIT;
```

The data-modifying CTE carries the unchanged attributes forward from the row it just closed, so they cannot drift apart.

### 7.3 Resolving `menu_sk` when loading sales

When a sale is loaded, `menu_sk` must resolve to the version in force **on the date of sale**:

```sql
-- CORRECT
SELECT m.menu_sk
FROM   dim_menu m
WHERE  m.item_name = :src_item_name
  AND  :src_sale_date BETWEEN m.effective_from AND m.effective_to;
```

```sql
-- WRONG — silently reprices all history
SELECT m.menu_sk
FROM   dim_menu m
WHERE  m.item_name = :src_item_name
  AND  m.is_current;
```

The incorrect version runs without error and returns exactly one row. It attributes every historical sale to today's price. The damage is invisible until totals are reconciled against something external.

---

## 8. Business logic and worked queries

### 8.1 Revenue

`gross_amount = quantity × unit_price`, stored on the row. Total revenue is `SUM(gross_amount)` over any filter. No join required unless the filter or grouping needs a menu attribute.

### 8.2 Price-change scenarios

The primary use case: *"if I had charged ₹X more for this item, what would I have earned?"*

```sql
WITH params AS (
    SELECT 'Paneer Butter Masala'::varchar AS item,
           20.00::numeric                  AS increase
)
SELECT m.item_name,
       sum(f.quantity)                              AS units_sold,
       sum(f.gross_amount)                          AS actual_revenue,
       sum(f.gross_amount) + sum(f.quantity) * p.increase
                                                    AS hypothetical_revenue,
       sum(f.quantity) * p.increase                 AS extra_revenue
FROM   fact_sales f
JOIN   dim_menu   m ON m.menu_sk = f.menu_sk
CROSS  JOIN params p
WHERE  m.item_name = p.item
  AND  f.sale_date >= DATE '2026-09-01'
  AND  f.sale_date <  DATE '2026-10-01'
GROUP  BY m.item_name, p.increase;
```

**Mandatory caveat.** This holds quantity constant — it assumes customers buy exactly as much at the higher price. In reality demand usually falls as price rises (price elasticity). The figure is an **upper bound**, not a forecast, and any answer built on it must say so.

Because kitchen costs do not change when a menu price changes, this extra revenue is also the extra profit. That is why the absence of a cost column does not block this question.

### 8.3 Testing that assumption against real history

SCD2 makes the naive assumption checkable. If the item's price has changed before, the data shows what actually happened to volume:

```sql
SELECT m.item_price,
       m.effective_from,
       m.effective_to,
       sum(f.quantity) AS units,
       round(sum(f.quantity)::numeric
             / (LEAST(m.effective_to, CURRENT_DATE) - m.effective_from + 1), 2)
                       AS units_per_day
FROM   fact_sales f
JOIN   dim_menu   m ON m.menu_sk = f.menu_sk
WHERE  m.item_name = 'Beef Fry'
GROUP  BY m.item_price, m.effective_from, m.effective_to
ORDER  BY m.effective_from;
```

`units_per_day` rather than raw `units`, because price periods are of unequal length and raw totals are not comparable across them. The `+ 1` is required because the range is inclusive at both ends. `LEAST(effective_to, CURRENT_DATE)` keeps the open row from dividing by six thousand years.

### 8.4 Bill-level metrics

```sql
-- Average bill value
SELECT round(avg(bill_total), 2) AS avg_bill_value
FROM (
    SELECT bill_number, sum(gross_amount) AS bill_total
    FROM   fact_sales
    WHERE  sale_date >= DATE '2026-09-01'
      AND  sale_date <  DATE '2026-10-01'
    GROUP  BY bill_number
) b;
```

Aggregate to `bill_number` first, then average.

### 8.5 Daypart analysis

`sale_time` makes service periods queryable. Trading runs from about 11:45 to 23:57, with a lunch peak around 13:00 and a heavier dinner peak around 20:00–21:00.

```sql
SELECT CASE
           WHEN sale_time <  TIME '16:00' THEN 'lunch'
           WHEN sale_time <  TIME '18:30' THEN 'tea'
           ELSE                                'dinner'
       END                         AS daypart,
       count(DISTINCT bill_number) AS bills,
       round(sum(gross_amount))    AS revenue
FROM   fact_sales
GROUP  BY 1
ORDER  BY revenue DESC;
```

### 8.6 Dine-in versus takeaway

`table_no IS NULL` marks a parcel order. About 24% of lines are takeaway.

```sql
SELECT CASE WHEN table_no IS NULL THEN 'parcel' ELSE 'dine-in' END AS channel,
       count(DISTINCT bill_number) AS bills,
       round(sum(gross_amount))    AS revenue
FROM   fact_sales
GROUP  BY 1;
```

### 8.7 Date filtering

Use half-open ranges for `sale_date`:

```sql
WHERE sale_date >= DATE '2026-09-01' AND sale_date < DATE '2026-10-01'
```

See §2.2 for why this differs from the `BETWEEN` used on version ranges.

---

## 9. What is loaded

| | |
|---|---|
| Period | 2024-10-01 to 2026-09-30 (724 trading days; 6 closed for hartals and maintenance) |
| `dim_menu` rows | 884 versions across 291 distinct items |
| Items on the menu today | 286 (5 discontinued during the period, 7 added) |
| Price revisions | 4 waves: 2025-01-15, 2025-06-01, 2025-11-10, 2026-04-01 |
| `fact_sales` rows | 186,897 lines across 52,811 bills |
| Revenue | ₹5.69 crore |
| Average bill | ₹1,076 across 3.54 lines |
| Trading hours | 11:45 to 23:57 |
| Takeaway share | 24% of lines |

Total revenue of **₹5.69 crore** is the reconciliation anchor. An unfiltered `SUM(gross_amount)` that does not match it means the query is wrong.

The data is synthetic but modelled on the real trading calendar of central Thrissur: weekday and weekend rhythm, the monsoon trough in June–July, the payday cycle, Thrissur Pooram, Onam, Ramadan and Eid, Lent and Easter, Christmas and New Year, and Vishu. Festival effects show up in both volume and product mix — Arabic dishes roughly double their revenue share during Ramadan, and the daypart inverts towards iftar.

Seven dishes on the printed menu are **absent** from the data: the four Arabic Grilled Fish marinades and three Tawa Fish marinades, all priced "Ask for price", which `item_price NOT NULL` cannot represent.

---

## 10. Caveats to attach to answers

Where a query touches one of these, the caveat belongs in the answer, not just in this file.

| Trigger | Caveat |
|---|---|
| Any price-change scenario | Assumes demand is unchanged; an upper bound, not a forecast |
| Any money figure | Call it revenue. It is gross — no cost, tax or service charge is deducted |
| Any veg / non-veg breakdown | State whether `meal_type` or `category` was used, and that they give very different answers |
| Any use of `serving_size` | An estimate of how many people a portion feeds, not a measured weight |
| A date range outside 2024-10-01 .. 2026-09-30 | Outside the loaded period — not a real decline |
| A `GROUP BY category` result | `arabic grilled fish` and `tawa fish` have no rows and will not appear |

---

## Implementation status

No views exist. Queries go directly against `dim_menu` and `fact_sales`, so §2.1, §2.2 and §2.3 are enforced by instruction rather than by structure. A flattened view over the join would enforce all three structurally; it can be added later without altering either table, and would be documented here to the same standard as §5 and §6.
