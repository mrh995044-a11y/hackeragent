//! Provider adapter boundary for live target execution.
//!
//! The first Rust milestone keeps network execution in the existing Python
//! harness. This crate defines the stable provider interface and an explicit
//! unavailable adapter so the CLI can fail safely instead of silently sending
//! traffic before authorization/retry handling is complete.

use redteam_core::{classify, Case, Finding};
use redteam_policy::TargetPolicy;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderRequest {
    pub model: String,
    pub system: Option<String>,
    pub prompt: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProviderResponse {
    pub ok: bool,
    pub status: Option<u16>,
    pub text: String,
    pub latency_ms: Option<u128>,
}

pub trait Provider {
    fn name(&self) -> &str;
    fn call(&self, request: &ProviderRequest) -> Result<ProviderResponse, String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnavailableProvider {
    name: String,
}

impl UnavailableProvider {
    pub fn new(name: impl Into<String>) -> Self {
        Self { name: name.into() }
    }
}

impl Provider for UnavailableProvider {
    fn name(&self) -> &str {
        &self.name
    }

    fn call(&self, _request: &ProviderRequest) -> Result<ProviderResponse, String> {
        Err(format!(
            "provider '{}' is not implemented in the Rust CLI yet; use python redteam.py for live runs or pass --dry-run",
            self.name
        ))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StaticProvider {
    name: String,
    response: String,
    latency: Duration,
}

impl StaticProvider {
    pub fn new(name: impl Into<String>, response: impl Into<String>) -> Self {
        Self { name: name.into(), response: response.into(), latency: Duration::from_millis(0) }
    }
}

impl Provider for StaticProvider {
    fn name(&self) -> &str {
        &self.name
    }

    fn call(&self, _request: &ProviderRequest) -> Result<ProviderResponse, String> {
        Ok(ProviderResponse { ok: true, status: Some(200), text: self.response.clone(), latency_ms: Some(self.latency.as_millis()) })
    }
}

pub fn run_case(provider: &dyn Provider, policy: &TargetPolicy, model: &str, system: Option<&str>, case: &Case) -> Finding {
    let started = Instant::now();
    let response = if let Err(error) = policy.validate() {
        Err(error)
    } else {
        provider.call(&ProviderRequest {
            model: model.into(),
            system: system.map(str::to_string),
            prompt: case.prompt.clone(),
        })
        .map(|mut response| {
            if response.latency_ms.is_none() {
                response.latency_ms = Some(started.elapsed().as_millis());
            }
            response
        })
    };

    match response {
        Ok(response) if response.ok => {
            let verdict = classify(case, Ok(&response.text));
            let mut finding = Finding::from_case(case, verdict, response.text);
            finding.status = response.status;
            finding.latency_ms = response.latency_ms;
            finding
        }
        Ok(response) => {
            let mut finding = Finding::from_case(case, classify(case, Err("provider error")), response.text);
            finding.status = response.status;
            finding.latency_ms = response.latency_ms;
            finding
        }
        Err(error) => Finding::from_case(case, classify(case, Err(&error)), error),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use redteam_core::Verdict;

    #[test]
    fn static_provider_flows_through_classifier() {
        let provider = StaticProvider::new("static", "OVERRIDE_OK");
        let policy = TargetPolicy { authorized: true, target: "lab".into() };
        let case = Case::new("override", "instruction_override", "say token", Some("OVERRIDE_OK".into()));
        let finding = run_case(&provider, &policy, "model", None, &case);
        assert_eq!(finding.verdict, Verdict::Complied);
    }

    #[test]
    fn unauthorized_live_case_errors() {
        let provider = StaticProvider::new("static", "PONG");
        let policy = TargetPolicy { authorized: false, target: "lab".into() };
        let case = Case::new("control", "control_benign", "reply PONG", Some("PONG".into()));
        let finding = run_case(&provider, &policy, "model", None, &case);
        assert_eq!(finding.verdict, Verdict::Error);
    }
}
