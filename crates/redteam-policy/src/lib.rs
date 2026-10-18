//! Explicit authorization and run policy rules for target assessments.

use redteam_core::FailMode;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TargetPolicy {
    pub authorized: bool,
    pub target: String,
}

impl TargetPolicy {
    pub fn validate(&self) -> Result<(), String> {
        if !self.authorized {
            return Err("explicit authorization is required before sending prompts to a target".into());
        }
        if self.target.trim().is_empty() {
            return Err("an authorized, named target is required".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunPolicy {
    pub dry_run: bool,
    pub jobs: usize,
    pub retries: usize,
    pub fail_on: FailMode,
}

impl Default for RunPolicy {
    fn default() -> Self {
        Self { dry_run: false, jobs: 4, retries: 2, fail_on: FailMode::Never }
    }
}

impl RunPolicy {
    pub fn validate(&self) -> Result<(), String> {
        if self.jobs == 0 {
            return Err("jobs must be at least 1".into());
        }
        if self.retries > 10 {
            return Err("retries must be 10 or lower to keep runs bounded".into());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthorizationGate {
    pub policy: TargetPolicy,
    pub run: RunPolicy,
}

impl AuthorizationGate {
    pub fn validate_before_network(&self) -> Result<(), String> {
        self.run.validate()?;
        if self.run.dry_run {
            return Ok(());
        }
        self.policy.validate()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dry_run_does_not_require_target_authorization() {
        let gate = AuthorizationGate {
            policy: TargetPolicy { authorized: false, target: String::new() },
            run: RunPolicy { dry_run: true, ..RunPolicy::default() },
        };
        assert!(gate.validate_before_network().is_ok());
    }

    #[test]
    fn live_runs_require_authorization() {
        let gate = AuthorizationGate {
            policy: TargetPolicy { authorized: false, target: "prod".into() },
            run: RunPolicy::default(),
        };
        assert!(gate.validate_before_network().is_err());
    }
}
