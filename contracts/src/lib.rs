// SanctionPay - On-chain Sanction Check Log Contract
// Built with Odra Framework for Casper Network
#![cfg_attr(not(test), no_std)]
#![cfg_attr(not(test), no_main)]
extern crate alloc;

use odra::prelude::*;
use odra::casper_types::U256;

/// A single sanction check result stored on-chain
#[odra::odra_type]
pub struct SanctionCheckResult {
    pub query_hash: String,       // SHA256 of the queried address/entity
    pub is_sanctioned: bool,      // True if sanctioned
    pub risk_score: u8,           // 0-100 risk score
    pub lists_matched: String,    // Comma-separated list names (OFAC, UN, EU...)
    pub timestamp: u64,           // Block timestamp
    pub checker: Address,         // Who requested the check
}

/// SanctionPay main contract
#[odra::module]
pub struct SanctionPay {
    /// Owner of the contract
    owner: Var<Address>,
    /// Total number of checks performed
    total_checks: Var<u64>,
    /// Mapping from query_hash to latest result
    results: Mapping<String, SanctionCheckResult>,
    /// Authorized backend addresses (can write results)
    authorized_writers: Mapping<Address, bool>,
    /// x402 payment amount in motes required per check
    price_per_check: Var<U256>,
    /// Accumulated fees
    total_fees_collected: Var<U256>,
}

#[odra::module]
impl SanctionPay {
    /// Initialize the contract
    pub fn init(&mut self, price_per_check: U256) {
        let caller = self.env().caller();
        self.owner.set(caller);
        self.total_checks.set(0u64);
        self.price_per_check.set(price_per_check);
        self.total_fees_collected.set(U256::zero());
        // Owner is always an authorized writer
        self.authorized_writers.set(&caller, true);
    }

    /// Add an authorized writer (only owner)
    pub fn add_writer(&mut self, writer: Address) {
        self.assert_owner();
        self.authorized_writers.set(&writer, true);
    }

    /// Remove an authorized writer (only owner)
    pub fn remove_writer(&mut self, writer: Address) {
        self.assert_owner();
        self.authorized_writers.set(&writer, false);
    }

    /// Record a sanction check result on-chain (only authorized writers)
    pub fn record_check(
        &mut self,
        query_hash: String,
        is_sanctioned: bool,
        risk_score: u8,
        lists_matched: String,
    ) {
        self.assert_authorized_writer();

        let timestamp = self.env().get_block_time();
        let checker = self.env().caller();

        let result = SanctionCheckResult {
            query_hash: query_hash.clone(),
            is_sanctioned,
            risk_score,
            lists_matched,
            timestamp,
            checker,
        };

        self.results.set(&query_hash, result);

        let current = self.total_checks.get_or_default();
        self.total_checks.set(current + 1);
    }

    /// Get a sanction check result by query hash
    pub fn get_result(&self, query_hash: String) -> Option<SanctionCheckResult> {
        self.results.get(&query_hash)
    }

    /// Get total number of checks
    pub fn total_checks(&self) -> u64 {
        self.total_checks.get_or_default()
    }

    /// Get price per check
    pub fn price_per_check(&self) -> U256 {
        self.price_per_check.get_or_default()
    }

    /// Update price per check (only owner)
    pub fn set_price(&mut self, new_price: U256) {
        self.assert_owner();
        self.price_per_check.set(new_price);
    }

    /// Transfer ownership
    pub fn transfer_ownership(&mut self, new_owner: Address) {
        self.assert_owner();
        self.owner.set(new_owner);
    }

    // ---- Internal helpers ----

    fn assert_owner(&self) {
        let caller = self.env().caller();
        let owner = self.owner.get().unwrap();
        if caller != owner {
            self.env().revert(SanctionPayError::NotOwner);
        }
    }

    fn assert_authorized_writer(&self) {
        let caller = self.env().caller();
        let is_authorized = self.authorized_writers.get(&caller).unwrap_or(false);
        if !is_authorized {
            self.env().revert(SanctionPayError::NotAuthorized);
        }
    }
}

/// Contract errors
#[odra::odra_error]
pub enum SanctionPayError {
    NotOwner = 1,
    NotAuthorized = 2,
    InsufficientPayment = 3,
}

#[cfg(test)]
mod tests {
    use super::*;
    use odra::host::{Deployer, HostRef};

    #[test]
    fn test_init() {
        let test_env = odra_test::env();
        let price = U256::from(1_000_000_000u64); // 1 CSPR in motes
        let contract = SanctionPay::deploy(&test_env, SanctionPayInitArgs {
            price_per_check: price,
        });
        assert_eq!(contract.total_checks(), 0u64);
        assert_eq!(contract.price_per_check(), price);
    }

    #[test]
    fn test_record_and_get() {
        let test_env = odra_test::env();
        let mut contract = SanctionPay::deploy(&test_env, SanctionPayInitArgs {
            price_per_check: U256::from(1_000_000_000u64),
        });

        let hash = String::from("abc123hash");
        contract.record_check(
            hash.clone(),
            true,
            85,
            String::from("OFAC-SDN,UN-SC"),
        );

        assert_eq!(contract.total_checks(), 1u64);

        let result = contract.get_result(hash).unwrap();
        assert!(result.is_sanctioned);
        assert_eq!(result.risk_score, 85);
        assert_eq!(result.lists_matched, "OFAC-SDN,UN-SC");
    }

    #[test]
    fn test_unauthorized_writer() {
        let test_env = odra_test::env();
        let mut contract = SanctionPay::deploy(&test_env, SanctionPayInitArgs {
            price_per_check: U256::from(1_000_000_000u64),
        });

        // Switch to a different account
        test_env.set_caller(test_env.get_account(1));

        // Should fail — caller is not an authorized writer
        let result = contract.try_record_check(
            String::from("hash"),
            false,
            0,
            String::from(""),
        );
        assert!(result.is_err());
    }
}
