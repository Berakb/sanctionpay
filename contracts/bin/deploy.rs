//! Deploy SanctionPay to a livenet (Casper Testnet) via odra-casper-livenet-env.
//!
//! Required env:
//!   ODRA_CASPER_LIVENET_NODE_ADDRESS     e.g. https://node.testnet.casper.network/rpc
//!   ODRA_CASPER_LIVENET_CHAIN_NAME       casper-test
//!   ODRA_CASPER_LIVENET_SECRET_KEY_PATH  path to the funded ed25519 secret_key.pem
//!
//! Run:  cargo run --bin sanctionpay_contract_deploy

use odra::casper_types::U256;
use odra::host::Deployer;
use odra::prelude::Addressable;
use sanctionpay_contract::{SanctionPay, SanctionPayInitArgs};

fn main() {
    let env = odra_casper_livenet_env::env();

    // Deploy gas ceiling (motes). A ~321 KB wasm install is costly; the account holds ~5000 CSPR.
    env.set_gas(700_000_000_000u64);

    let init_args = SanctionPayInitArgs {
        price_per_check: U256::from(10_000_000u64), // 0.01 CSPR per check
    };

    let mut contract = SanctionPay::deploy(&env, init_args);
    println!("DEPLOYED_CONTRACT_ADDRESS={}", contract.address().to_string());

    // Sample on-chain screening records (deployer == owner == authorized writer).
    env.set_gas(5_000_000_000u64);
    contract.record_check(
        "b1946ac92492d2347c6235b4d2611184".to_string(),
        true,
        90u8,
        "UN-SC".to_string(),
    );
    println!("RECORDED_SANCTIONED_OK");

    env.set_gas(5_000_000_000u64);
    contract.record_check(
        "5d41402abc4b2a76b9719d911017c592".to_string(),
        false,
        5u8,
        "".to_string(),
    );
    println!("RECORDED_CLEAR_OK");

    println!("TOTAL_CHECKS={}", contract.total_checks());
}
