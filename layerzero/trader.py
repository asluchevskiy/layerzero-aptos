# -*- coding: utf-8 -*-
import csv
import json
import logging
import math
import os
import random
import time

from aptos_sdk.account import Account as AptosAccount
from aptos_sdk.client import RestClient, ResourceNotFound
from aptos_sdk.transactions import EntryFunction, TransactionPayload
from aptos_sdk.type_tag import StructTag, TypeTag

from layerzero.api import Node, Account, Contract
from layerzero.filereader import CsvFileReader
from layerzero.logger import setup_color_logging, setup_file_logging
from layerzero.utils import random_float
from layerzero.web3 import Web3


class CsvFileReaderWithCheck(CsvFileReader):
    def check(self, res) -> list:
        for item in res:
            Web3().eth.account.from_key(item['private_key'])  # checking private key
            AptosAccount.load_key(item['aptos_private_key'])  # checking aptos private key
        return res


class AptosBridge(Contract):

    APTOS_GAS = 10000
    APTOS_AIRDROP = 520400

    def __init__(self, node):
        chain_id_to_address = {
            1: '0x50002cdfe7ccb0c41f519c6eb0653158d11cd907',
            10: '0x86Bb63148d17d445Ed5398ef26Aa05Bf76dD5b59',
            42161: '0x1BAcC2205312534375c8d1801C27D28370656cFf'
        }
        address = chain_id_to_address.get(node.chain_id)
        if not address:
            raise RuntimeError(f'network chain_id={node.chain_id} is not supported')
        super().__init__(node, 'layerzero_aptos', address)

    def _get_call_params(self, address: str):
        call_params = [address, '0x0000000000000000000000000000000000000000']
        return call_params

    def _get_adapter_params(self, aptos_address: str):
        adapter_params = Web3.solidity_pack(
            ["uint16", "uint256", "uint256", "bytes"],
            [2, self.APTOS_GAS, self.APTOS_AIRDROP, aptos_address]
        )
        return adapter_params

    def quote_to_send(self, account: Account, aptos_address: str):
        return self.functions.quoteForSend(self._get_call_params(account.address),
                                           self._get_adapter_params(aptos_address)).call()

    def send_eth_to_aptos(self, account: Account, aptos_address: str, amount: int, transfer_amount: int):
        tx = self.functions.sendETHToAptos(
            aptos_address,
            amount,
            self._get_call_params(account.address),
            self._get_adapter_params(aptos_address),
        )
        tx = account.build_transaction(tx, transfer_amount)
        signed_tx = account.sign_transaction(tx)
        return self._node.send_raw_transaction(signed_tx.rawTransaction)


class AptosNode:

    MAX_GAS_AMOUNT = 1_000

    def __init__(self, rpc_url, explorer_url):
        self.client = RestClient(base_url=rpc_url)
        if not explorer_url.endswith('/'):
            explorer_url += '/'
        self.explorer_url = explorer_url

    def get_balance(self, account):
        try:
            return int(self.client.account_balance(account.address()))
        except ResourceNotFound:
            return 0

    def get_explorer_transaction_url(self, tx_hash: str):
        return f'{self.explorer_url}txn/{tx_hash}'

    def get_explorer_address_url(self, address: str):
        return f'{self.explorer_url}account/{address}'

    def send_transaction(self, account: AptosAccount, payload: TransactionPayload):
        self.client.client_config.max_gas_amount = self.MAX_GAS_AMOUNT
        raw_tx = self.client.create_bcs_transaction(account, payload)
        simulate_res = self.client.simulate_transaction(raw_tx, account)
        gas_limit = math.ceil(int(simulate_res[0]['gas_used']) * 1.2)
        self.client.client_config.max_gas_amount = gas_limit
        signed_tx = self.client.create_bcs_signed_transaction(account, payload)
        tx_hash = self.client.submit_bcs_transaction(signed_tx)
        return tx_hash


class LayerzeroTrader:
    CONFIG_FILENAME = os.environ.get('CONFIG_FILENAME', 'config.json')

    def __init__(self):
        with open(self.CONFIG_FILENAME) as f:
            self.config = json.load(f)

        self.logger = logging.getLogger('layerzero')
        self.logger.setLevel(logging.DEBUG)
        setup_file_logging(self.logger, self.config['log_file'])
        setup_color_logging(self.logger)

        network_name = self.config['working_network']
        self.node = Node(rpc_url=self.config['networks'][network_name]['rpc'],
                         explorer_url=self.config['networks'][network_name]['explorer'])
        self.aptos_node = AptosNode(rpc_url=self.config['networks']['aptos']['rpc'],
                                    explorer_url=self.config['networks']['aptos']['explorer'])
        self.aptos_client = self.aptos_node.client
        self.max_gwei = self.config['networks'][network_name].get('max_gwei')

    @staticmethod
    def load_wallets(filename):
        res = []
        with open(filename) as f:
            reader = csv.reader(f)
            for row in reader:
                res.append(row)  # todo: check
        return res

    def withdraw(self, account, aptos_account, amount):
        b = AptosBridge(self.node)
        aptos_address = aptos_account.address().hex()
        quote_to_send_res = b.quote_to_send(account, aptos_address)
        amount_wei = Web3.to_wei(amount, 'ether')
        native_fee = quote_to_send_res[0]
        transfer_amount = amount_wei + native_fee
        if account.balance_in_wei < transfer_amount:
            raise ValueError(f'low balance: {account.balance} ETH < {Web3.from_wei(transfer_amount, "ether")} ETH')
        self.logger.debug(f'Transferring {amount} ETH -> Aptos...')
        tx_hash = b.send_eth_to_aptos(account, aptos_address, amount_wei, transfer_amount)
        return tx_hash

    def _aptos_tx_register(self, account):
        payload = TransactionPayload(
            EntryFunction.natural(
                module="0x1::managed_coin",
                function="register",
                ty_args=[
                    TypeTag(StructTag.from_str(
                        f'0xf22bede237a07e121b56d91a491eb7bcdfd1f5907926a9e58338f964a01b17fa::asset::WETH'))
                ],
                args=[]
            )
        )
        return self.aptos_node.send_transaction(account, payload)

    def _aptos_tx_claim(self, account):
        payload = TransactionPayload(
            EntryFunction.natural(
                module="0xf22bede237a07e121b56d91a491eb7bcdfd1f5907926a9e58338f964a01b17fa::coin_bridge",
                function="claim_coin",
                ty_args=[
                    TypeTag(StructTag.from_str(
                        f'0xf22bede237a07e121b56d91a491eb7bcdfd1f5907926a9e58338f964a01b17fa::asset::WETH'))
                ],
                args=[]
            )
        )
        return self.aptos_node.send_transaction(account, payload)

    def run(self):
        try:
            wallets = CsvFileReaderWithCheck(self.config['wallets_file']).load()
        except Exception as ex:
            self.logger.error(ex)
            return
        for i, wallet in enumerate(wallets, 1):
            try:
                account = Account(node=self.node, private_key=wallet['private_key'])
                # aptos_account = AptosNode.get_account(wallet['aptos_private_key'])
                aptos_account = AptosAccount.load_key(wallet['aptos_private_key'])
                balance = account.balance
                try:
                    aptos_balance = int(self.aptos_client.account_balance(aptos_account.address()))
                    is_new_account = False
                except ResourceNotFound:
                    aptos_balance = 0
                    is_new_account = True
                self.logger.info(f'{account.address} ({balance} ETH)')
                self.logger.debug(self.node.get_explorer_address_url(account.address))
                new_acc_str = ' NEW ACCOUNT' if is_new_account else ''
                self.logger.info(f'{aptos_account.address()} ({aptos_balance / 10 ** 8} APT){new_acc_str}')
                self.logger.debug(self.aptos_node.get_explorer_address_url(aptos_account.address().hex()))

                random_amount = random_float(*self.config['amount']['ETH'], diff=2)
                if self.max_gwei:
                    self.node.wait_for_gas(self.max_gwei, logger=self.logger)

                tx_hash = self.withdraw(account=account, aptos_account=aptos_account, amount=random_amount)
                self.logger.debug(self.node.get_explorer_transaction_url(tx_hash))
                try:
                    sequence_number = self.aptos_client.account_sequence_number(aptos_account.address())
                except Exception as ex:
                    sequence_number = 0
                if is_new_account or not sequence_number:
                    balance = self.aptos_node.get_balance(aptos_account)
                    if not balance:
                        self.logger.debug('waiting for APT deposit...')
                    while True:
                        if balance:
                            self.logger.debug('sending REGISTER & CLAIM transactions...')
                            tx_hash = self._aptos_tx_register(aptos_account)
                            self.logger.debug(self.aptos_node.get_explorer_transaction_url(tx_hash))
                            self.aptos_client.wait_for_transaction(tx_hash)
                            self.random_delay(*self.config['delay']['transaction'])
                            tx_hash = self._aptos_tx_claim(aptos_account)
                            self.logger.debug(self.aptos_node.get_explorer_transaction_url(tx_hash))
                            break
                        else:
                            self.random_delay(15, 30)
                            balance = self.aptos_node.get_balance(aptos_account)

            except Exception as ex:
                self.logger.error(ex)
            if i < len(wallets):
                self.random_delay(*self.config['delay']['wallet'])

    def random_delay(self, min_sec, max_sec):
        random_sec = random.randint(min_sec, max_sec)
        self.logger.debug(f'delay for {random_sec} sec')
        time.sleep(random_sec)
