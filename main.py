import argparse
import csv
import sqlite3
from typing import NamedTuple, List, Dict, Any, Tuple

import yaml


def main():
    parser = argparse.ArgumentParser(description='Make a Sankey diagram out of exported Mint transaction data')
    parser.add_argument('--config', default='config.yaml', help='Path to config file (default config.yaml)')
    parser.add_argument(
        '--mode', choices=['sankey', 'flat'], default='sankey',
        help='sankey to print Sankeymatic-formatted output, '
             'flat for just a flat list of net spending by category (useful for creating a category hierarchy config)',
    )
    parser.add_argument(
        '--onlySpend', action='store_true',
        help='Only categorize spending (leave income, savings, and taxes out of the final diagram)',
    )
    args = parser.parse_args()

    config = read_config(args.config)
    conn = sqlite3.connect(config.db_file)
    cur = conn.cursor()

    load_transactions(config.transactions_file, cur)
    conn.commit()

    conf_exclude = (config.exclude_categories, config.exclude_accounts, config.exclude_labels)
    net_spend_by_category = select_sums(cur, config.category_offsets, *conf_exclude)

    if args.mode == 'flat':
        for cat, v in net_spend_by_category.items():
            print(f'{cat}: {v}')
        return

    spend_diagram, total_spend = sankey_spending(net_spend_by_category, config.categories, config.min_category_amount)
    if not args.onlySpend:
        net_income_by_cat = {k: int(v) for k, v in net_spend_by_category.items() if k in config.net_income_categories}
        net_income = -sum(net_income_by_cat.values())
        for k, v in net_income_by_cat.items():
            spend_diagram += f'{k} [{-v}] Net Income\n'
        spend_diagram += f'Net Income [{total_spend}] Spending\n'

        savings_nodes, saving_val = rollup_subcat(net_spend_by_category, 'Net Income', 'Savings', config.saving_categories)
        tax_nodes, tax_val = rollup_subcat(net_spend_by_category, 'Net Income', 'Taxes', config.tax_categories)
        if len(savings_nodes) > 0:
            spend_diagram += f'{savings_nodes}\n'
        if len(tax_nodes) > 0:
            spend_diagram += f'{tax_nodes}\n'

        income_diff = net_income - total_spend - saving_val - tax_val
        if income_diff != 0:
            print(f'NOTE: There is a ${income_diff} gap between net income and spending '
                  f'(a negative value means you spent more than income). This will show up as a "Yearly Deficit" or '
                  f'"Yearly Surplus" node in the final graph to ensure node flows line up properly.\n')
            if income_diff > 0:
                spend_diagram += f'Net Income [{income_diff}] Yearly Surplus\n'
            else:
                spend_diagram += f'Yearly Deficit [{-income_diff}] Net Income\n'

    print(spend_diagram)


class Transaction(NamedTuple):
    date: str
    merchant: str
    category: str
    account: str
    original_statement: str
    notes: str
    amount: float
    # Because I'm being lazy and packing CSV labels into 1 field, excluding labels doesn't work if there are multiple
    # on a single transaction
    tags: str

    def __conform__(self, protocol):
        if protocol is sqlite3.PrepareProtocol:
            return self.date,  self.merchant, self.category, self.account, self.original_statement, self.notes, \
                self.amount, self.tags


class Config(NamedTuple):
    transactions_file: str
    db_file: str
    min_category_amount: int
    categories: Dict[str, Any]
    net_income_categories: Dict[str, Any]
    exclude_categories: List[str]
    exclude_accounts: List[str]
    exclude_labels: List[str]
    category_offsets: Dict[str, int]
    saving_categories: List[str]
    tax_categories: List[str]


def read_config(filename: str) -> Config:
    with open(filename, 'r') as f:
        config = yaml.load(f, Loader=yaml.Loader)

    required_keys = [
        'transactions_file',
        'db_file',
        'categories',
        'min_category_amount',
        'net_income_categories',
        'exclude_categories',
        'exclude_accounts',
        'exclude_labels',
        'category_offsets',
        'saving_categories',
        'tax_categories',
    ]
    if any(k not in config for k in required_keys):
        raise Exception(f'Improper config.yaml\nconfig.yaml needs the following keys defined: {required_keys}')
    if any(k not in required_keys for k in config):
        raise Exception(f'Improper config.yaml\nconfig.yaml expects only the following keys: {required_keys}')
    return Config(**config)


def load_transactions(tx_filename: str, cur: sqlite3.Cursor):
    txs = []
    with open(tx_filename, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            tx = Transaction(
                date=row['Date'],
                merchant=row['Merchant'],
                category=row['Category'],
                account=row['Account'],
                original_statement=row['Original Statement'],
                notes=row['Notes'],
                amount=float(row['Amount']),
                tags=row['Tags'],
            )
            txs.append(tx)

    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS transactions(
            date TEXT,
            merchant TEXT,
            category TEXT,
            account TEXT,
            original_statement TEXT,
            notes TEXT,
            amount REAL,
            tags TEXT
        )
    ''')
    cur.execute('''DELETE FROM transactions WHERE TRUE''')
    cur.executemany(f'INSERT INTO transactions VALUES(?, ?, ?, ?, ?, ?, ?, ?)', txs)


def select_sums(
        cur: sqlite3.Cursor,
        category_offsets: Dict[str, int],
        exclude_categories: List[str],
        exclude_accounts: List[str],
        exclude_tags: List[str],
) -> Dict[str, int]:
    category_placeholder = _get_palceholder_vals(len(exclude_categories))
    account_placeholder = _get_palceholder_vals(len(exclude_accounts))
    tags_placeholder = _get_palceholder_vals(len(exclude_tags))

    res = cur.execute(f'''
        SELECT category, SUM(amount) FROM transactions
        WHERE
            category NOT IN {category_placeholder} AND
            account NOT IN {account_placeholder} AND
            tags NOT IN {tags_placeholder}
        GROUP BY category
        ORDER BY category
    ''', (*exclude_categories, *exclude_accounts, *exclude_tags))

    ret = {}
    for row in res:
        ret[row[0]] = -int(float(row[1]))
    for k, v in category_offsets.items():
        if k not in ret:
            ret[k] = 0
        ret[k] += v
    return ret


def sankey_spending(net_spend: Dict[str, int], category_hierarchy: Dict[str, Any], category_limit: int) -> Tuple[str, int]:
    return _sankey_category(net_spend, '', 'Spending', category_hierarchy, category_limit)


def _sankey_category(data: Dict[str, int], parent: str, cat_name: str, cat_hier: Dict[str, Any], limit: int) -> Tuple[str, int]:
    # Base case: this is a leaf category for which we should have direct spend amount
    if cat_hier is None or len(cat_hier) == 0:
        amt = data.get(cat_name)
        if amt is None:
            raise Exception(f'No spend amount found for leaf category {cat_name}')

        # If under the lower limit, this needs to roll up to a grab-all misc subcategory
        if amt < limit:
            return '', amt
        else:
            return f'{parent} [{amt}] {cat_name}\n', amt

    # Recurse into subcategories
    ret = ''
    category_total = 0
    misc_subcategory_total = 0
    for subcatname, subcat in cat_hier.items():
        substring, subtotal = _sankey_category(data, cat_name, subcatname, subcat, limit)
        if substring == '':
            # Roll this into a misc subcategory
            misc_subcategory_total += subtotal

        ret += substring
        category_total += subtotal

    if misc_subcategory_total > 0:
        ret += f'{cat_name} [{misc_subcategory_total}] Misc. {cat_name} spending\n'

    if parent != '':
        ret += f'{parent} [{category_total}] {cat_name}\n\n'
    return ret, category_total


def rollup_subcat(net_spend: Dict[str, int], parent_cat: str, category_name: str, category_filter: List[str]) -> Tuple[str, int]:
    ret = ''
    by_cat = {k: v for k, v in net_spend.items() if k in category_filter}
    if len(by_cat) > 0:
        ret += f'{parent_cat} [{sum(by_cat.values())}] {category_name}\n'
        for k, v in by_cat.items():
            ret += f'{category_name} [{v}] {k}\n'
    return ret, sum(by_cat.values())


def _get_palceholder_vals(n: int) -> str:
    return f"({','.join(['?'] * n)})"


if __name__ == '__main__':
    main()
