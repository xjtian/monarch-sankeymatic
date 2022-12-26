Mint-Sankey
===

Turn your Mint transaction data into a Sankeymatic diagram with a customizable node hierarchy.

Install
---

Clone/download this repository, then

```
pip install -r requirements.txt
cp example-config.yaml config.yaml
```

Export Your Data
---

- Apply a date filter to the Mint transactions view to the time period you want to analyze
- Hit "Export xxx Transactions", which will download a `transactions.csv` file
- Drag that file into this directory, or just keep a note of where it is

Configure the Script
---

- Open `config.yaml` in your favorite editor
- Point `transactions_file` to wherever your `transactions.csv` is
- If you'd like to persist the DB table of your transactions, set `db_file` to something other than `:memory:`

Grab a list of all your transaction categories first:

```
$ python main.py --mode=raw

Shopping: 100
Mortgage & Rent: 1000
...
Paycheck: -5000
```

Using this data, you can construct your category hierarchy under the `categories` config key.
This is also a good time to set `exclude_categories`, `exclude_accounts`, and `exclude_labels` if you want to.

Run
---

```
$ python main.py

Food [100] Coffee Shops
Food [50] Fast Food
Food [400] Groceries
Food [200] Restaurants
Spending [750] Food

Home [100] Home Insurance
Home [1000] Mortgage & Rent
Home [200] Property Tax
Bills [1300] Home

Subscriptions [100] Business Services
Subscriptions [200] Streaming
Bills [300] Subscriptions

All Utilities [50] Internet
All Utilities [100] Mobile Phone
All Utilities [100] Utilities
Bills [250] All Utilities

Spending [1850] Bills
```

Paste the output into https://sankeymatic.com/ for your graph.