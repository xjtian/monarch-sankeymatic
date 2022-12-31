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
    spending_by_category = select_sums(cur, 'debit', *conf_exclude)
    credit_by_category = select_sums(cur, 'credit', *conf_exclude)
    net_spend = calculate_net_spend(spending_by_category, credit_by_category, config.category_offsets)

    if args.mode == 'flat':
        for cat, v in net_spend.items():
            print(f'{cat}: {v}')
        return

    spend_diagram, total_spend = sankey_spending(net_spend, config.categories, config.min_category_amount)
    if not args.onlySpend:
        net_income_by_cat = {k: int(v) for k, v in credit_by_category.items() if k in config.net_income_categories}
        net_income = sum(net_income_by_cat.values())
        for k, v in net_income_by_cat.items():
            spend_diagram += f'{k} [{v}] Net Income\n'
        spend_diagram += f'Net Income [{total_spend}] Spending\n'

        savings_nodes, saving_val = rollup_subcat(net_spend, 'Net Income', 'Savings', config.saving_categories)
        tax_nodes, tax_val = rollup_subcat(net_spend, 'Net Income', 'Taxes', config.tax_categories)
        spend_diagram += f'{savings_nodes}\n{tax_nodes}'

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
    description: str
    original_description: str
    amount: float
    type: str
    category: str
    account: str
    # Because I'm being lazy and packing CSV labels into 1 field, excluding labels doesn't work if there are multiple
    # on a single transaction
    labels: str

    def __conform__(self, protocol):
        if protocol is sqlite3.PrepareProtocol:
            return self.date, self.description, self.original_description, self.amount, self.type, self.category, \
                   self.account, self.labels


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
                date=row['Date'].replace('/', '-'),
                description=row['Description'],
                original_description=row['Original Description'],
                amount=float(row['Amount']),
                type=row['Transaction Type'],
                category=row['Category'],
                account=row['Account Name'],
                labels=row['Labels'],
            )
            txs.append(tx)

    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS transactions(
            date TEXT,
            description TEXT,
            original_description TEXT,
            amount REAL,
            type TEXT,
            category TEXT,
            account TEXT,
            labels TEXT
        )
    ''')
    cur.execute('''DELETE FROM transactions WHERE TRUE''')
    cur.executemany(f'INSERT INTO transactions VALUES(?, ?, ?, ?, ?, ?, ?, ?)', txs)


def select_sums(
        cur: sqlite3.Cursor,
        tx_type: str,
        exclude_categories: List[str],
        exclude_accounts: List[str],
        exclude_labels: List[str],
) -> Dict[str, float]:
    category_placeholder = _get_palceholder_vals(len(exclude_categories))
    account_placeholder = _get_palceholder_vals(len(exclude_accounts))
    labels_placeholder = _get_palceholder_vals(len(exclude_labels))

    res = cur.execute(f'''
        SELECT category, SUM(amount) FROM transactions
        WHERE
            type=? AND
            category NOT IN {category_placeholder} AND
            account NOT IN {account_placeholder} AND
            labels NOT IN {labels_placeholder}
        GROUP BY category
        ORDER BY category
    ''', (tx_type, *exclude_categories, *exclude_accounts, *exclude_labels))

    ret = {}
    for row in res:
        ret[row[0]] = float(row[1])
    return ret


def calculate_net_spend(spending_by_cat: Dict[str, float], credit_by_cat: Dict[str, float], offsets: Dict[str, int]) -> Dict[str, int]:
    net_spend = {}
    for k, v in spending_by_cat.items():
        net = v - credit_by_cat.get(k, 0)
        net_spend[k] = int(net)
    for k, v in credit_by_cat.items():
        if k not in net_spend:
            net_spend[k] = -int(v)

    for k, v in offsets.items():
        if k not in net_spend:
            net_spend[k] = 0
        net_spend[k] += v
    return net_spend


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
