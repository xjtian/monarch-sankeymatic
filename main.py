import argparse
import csv
import sqlite3
from typing import NamedTuple, List, Dict, Any, Tuple
import yaml


def main():
    parser = argparse.ArgumentParser(description='Make a Sankey diagram out of exported Mint transaction data')
    parser.add_argument('--config', default='config.yaml', help='Path to config file (default config.yaml)')
    parser.add_argument(
        '--mode', choices=['sankey', 'raw'], default='sankey',
        help='sankey to print Sankeymatic-formatted output, '
             'raw for just a flat list of net spending by category (useful for creating a category hierarchy config)',
    )
    args = parser.parse_args()

    config = read_config(args.config)
    conn = sqlite3.connect(config.db_file)
    cur = conn.cursor()

    load_transactions(config.transactions_file, cur)
    conn.commit()

    spending_by_category = select_sums(cur, 'debit', config)
    income_by_category = select_sums(cur, 'credit', config)
    net_spend = calculate_net_spend(spending_by_category, income_by_category)

    if args.mode == 'sankey':
        spend_diagram = sankey_spending(net_spend, config.categories, category_limit=500)[0]
        print(spend_diagram)
    else:
        for cat, v in net_spend.items():
            print(f'{cat}: {v}')


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
    categories: Dict[str, Any]
    exclude_categories: List[str]
    exclude_accounts: List[str]
    exclude_labels: List[str]


def read_config(filename: str) -> Config:
    with open(filename, 'r') as f:
        config = yaml.load(f, Loader=yaml.Loader)

    required_keys = [
        'transactions_file',
        'db_file',
        'categories',
        'exclude_categories',
        'exclude_accounts',
        'exclude_labels',
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


def select_sums(cur: sqlite3.Cursor, tx_type: str, config: Config) -> Dict[str, float]:
    category_placeholder = _get_palceholder_vals(len(config.exclude_categories))
    account_placeholder = _get_palceholder_vals(len(config.exclude_accounts))
    labels_placeholder = _get_palceholder_vals(len(config.exclude_labels))

    res = cur.execute(f'''
        SELECT category, SUM(amount) FROM transactions
        WHERE
            type=? AND
            category NOT IN {category_placeholder} AND
            account NOT IN {account_placeholder} AND
            labels NOT IN {labels_placeholder}
        GROUP BY category
        ORDER BY category
    ''', (tx_type, *config.exclude_categories, *config.exclude_accounts, *config.exclude_labels))

    ret = {}
    for row in res:
        ret[row[0]] = float(row[1])
    return ret


def calculate_net_spend(spending_by_cat: Dict[str, float], income_by_cat: Dict[str, float]) -> Dict[str, int]:
    net_spend = {}
    for k, v in spending_by_cat.items():
        net = v - income_by_cat.get(k, 0)
        net_spend[k] = int(net)
    for k, v in income_by_cat.items():
        if k not in net_spend:
            net_spend[k] = -int(v)

    return net_spend


def sankey_spending(net_spend: Dict[str, int], category_hierarchy: Dict[str, Any], category_limit: int = 500) -> Tuple[str, int]:
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


def _get_palceholder_vals(n: int) -> str:
    return f"({','.join(['?'] * n)})"


if __name__ == '__main__':
    main()
