                  Research Orchestrator
                          │
                 Workflow / State Store
                          │
          ┌───────────────┴───────────────┐
          │                               │
    Research Agents                Research Memory
          │                               │
    ├── Literature Research        ├── Hypothesis Registry
    ├── Market Research            ├── Experiment History
    ├── Hypothesis Generation      └── Failure Knowledge
    └── Strategy Implementation
          │
          ▼
    Experiment Specification
          │
          ▼
    Experiment Gatekeeper
          │
          ▼
    Experiment Infrastructure
          │
    ├── Versioned Data Pipeline
    ├── Backtesting Engine
    ├── Statistical Validation
    └── Experiment Tracking
          │
          ▼
    Evaluation Engine
          │
    ├── Performance Attribution
    ├── Robustness Analysis
    ├── Failure Diagnosis
    └── Strategy Selection
          │
          ├─────────────────────┐
          │                     │
          ▼                     ▼
    Research Feedback      Strategy Registry
          │                     │
          ▼                     ▼
    Research Orchestrator  Promotion Gate
                                │
                          Paper Trading
                                │
                          Shadow Deployment
                                │
                          Production Boundary
                                │
                          ├── Risk Engine
                          ├── Execution System
                          └── Monitoring
                              & Reconciliation