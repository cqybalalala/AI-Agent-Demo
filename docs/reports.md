## Customer / Supplier Totals
- Do NOT use reports for customer or supplier totals — report values are unreliable
- Use erpnext_list with group_by="customer" or group_by="supplier" and sum_field="grand_total"

## Purchase Analytics
- Purpose: Item-level purchase breakdown by month/quarter/year
- filters: company, from_date, to_date, doc_type, value_quantity, range
- doc_type options: "Purchase Order", "Purchase Invoice"
- value_quantity options: "Amount", "Quantity"
- range options: "Monthly", "Quarterly", "Yearly"
- example: {"company": "Penang Components Sdn Bhd", "from_date": "2025-01-01", "to_date": "2025-12-31", "doc_type": "Purchase Invoice", "value_quantity": "Amount", "range": "Monthly"}

## Sales Analytics
- Purpose: Item-level sales breakdown by month/quarter/year
- filters: company, from_date, to_date, doc_type, value_quantity, range
- doc_type options: "Sales Order", "Sales Invoice", "Delivery Note"
- value_quantity options: "Amount", "Quantity"
- range options: "Monthly", "Quarterly", "Yearly"
- example: {"company": "Penang Components Sdn Bhd", "from_date": "2025-01-01", "to_date": "2025-12-31", "doc_type": "Sales Invoice", "value_quantity": "Amount", "range": "Monthly"}

## Gross Profit
- Purpose: Gross profit and margin per invoice, item, or customer
- filters: company, from_date, to_date, group_by
- group_by options: "Invoice", "Item Code", "Customer", "Customer Group", "Brand", "Item Group", "Sales Person", "Territory"
- IMPORTANT: must include group_by or report returns empty
- example: {"company": "Penang Components Sdn Bhd", "from_date": "2025-01-01", "to_date": "2025-12-31", "group_by": "Item Code"}

## Accounts Receivable
- Purpose: Outstanding customer invoices and ageing
- filters: company, report_date, ageing_based_on, range1, range2, range3, range4
- ageing_based_on options: "Due Date", "Posting Date"
- report_date: single date (not a range), e.g. "2025-12-31"
- example: {"company": "Penang Components Sdn Bhd", "report_date": "2025-12-31", "ageing_based_on": "Due Date"}

## Accounts Payable
- Purpose: Outstanding supplier invoices and ageing
- filters: company, report_date, ageing_based_on
- ageing_based_on options: "Due Date", "Posting Date"
- example: {"company": "Penang Components Sdn Bhd", "report_date": "2025-12-31", "ageing_based_on": "Due Date"}

## Item-wise Sales History
- Purpose: All sales transactions per item
- filters: company, from_date, to_date, item_code (optional)
- IMPORTANT: for set analysis (comparing bought vs sold), always set top_n=0 to get ALL items, not just top 20
- example: {"company": "Penang Components Sdn Bhd", "from_date": "2025-01-01", "to_date": "2025-12-31"}

## Item-wise Purchase History
- Purpose: All purchase transactions per item
- filters: company, from_date, to_date, item_code (optional)
- IMPORTANT: for set analysis (comparing bought vs sold), always set top_n=0 to get ALL items, not just top 20
- example: {"company": "Penang Components Sdn Bhd", "from_date": "2025-01-01", "to_date": "2025-12-31"}

## Set Analysis (bought vs sold)
- For "items bought but not sold" or "items sold but not bought" queries:
  1. erpnext_items(parent_doctype="Purchase Invoice", filters=..., group_by="item_code") → bought set
  2. erpnext_items(parent_doctype="Sales Invoice", filters=..., group_by="item_code") → sold set
  3. Compare the two item_code lists to find differences
- Do NOT use Item-wise Purchase/Sales History reports — they may include non-invoice transactions

## Stock Balance
- Purpose: Current stock levels per item and warehouse
- filters: company, from_date, to_date, warehouse (optional), item_code (optional)
- example: {"company": "Penang Components Sdn Bhd", "to_date": "2025-12-31"}

## Stock Ledger
- Purpose: All stock movements (in/out) per item
- filters: company, from_date, to_date, warehouse (optional), item_code (optional)
- example: {"company": "Penang Components Sdn Bhd", "from_date": "2025-01-01", "to_date": "2025-12-31"}
