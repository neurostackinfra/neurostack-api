// --- Types ---
export type SRMDecisionStage = string;
export type SRMDecisionIntent = string;

export interface SRMDecision {
  intent: SRMDecisionIntent;
  stage: SRMDecisionStage;
  action: string;
  confidence: number;
  constraints: string[];
  gate_authority_before_price: boolean;
  safety_concern: boolean;
  handoff_required: boolean;
}

export interface TraceContextItem {
  key: string;
  value: string;
  source: string;
  title: string;
  field: string;
}

export interface DecisionTrace {
  id: string;
  timestamp: string;
  userMessage: string;
  srmDecision: SRMDecision;
  selectedContext: TraceContextItem[];
  promptBuilt: string;
  llmResponse: string;
  processingTimeMs: number;
  eventData?: Record<string, unknown>;
  eventMetadata?: Record<string, unknown>;
  fullContext?: Record<string, unknown>;
  fullPrompt?: Record<string, unknown>;
}
