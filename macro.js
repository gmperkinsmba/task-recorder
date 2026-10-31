"use strict";
const SAFE_ACTIONS = new Set(["add-item", "mark-ready"]);
const SAFE_FIELDS = new Set(["work-code", "summary"]);
const SENSITIVE = /password|passcode|email|phone|ssn|card|token|secret|api.?key/i;
const SENSITIVE_VALUE = /[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\d{3}-\d{2}-\d{4}\b|\b(?:\d[ -]?){13,19}\b|\b(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}\b|\b(?:sk|pk|api|token)[-_][a-z0-9_-]{8,}\b/i;

function isSensitive(input) {
  if (!input) return true;
  const type = (input.type || "text").toLowerCase();
  const autocomplete = (input.autocomplete || "").toLowerCase();
  const name = `${input.name || ""} ${input.id || ""} ${input.dataset?.field || ""}`;
  return type === "password" || type === "email" || input.dataset?.sensitive === "true" || /password|one-time-code|cc-|email|tel/i.test(autocomplete) || SENSITIVE.test(name);
}
function isSensitiveValue(value) { return typeof value !== "string" || SENSITIVE_VALUE.test(value); }

function validateStep(step, target="crm-demo") {
  if (!step || step.target !== target) return false;
  if (step.type === "input") return SAFE_FIELDS.has(step.field) && typeof step.value === "string" && step.value.length <= 240 && !isSensitiveValue(step.value);
  if (step.type === "click") return SAFE_ACTIONS.has(step.action) && (step.action === "add-item" || (typeof step.key === "string" && /^[a-z0-9][a-z0-9._-]{0,23}$/i.test(step.key) && !isSensitiveValue(step.key)));
  if (step.type === "scroll") return Number.isFinite(step.value) && step.value >= 0 && step.value <= 10000;
  if (step.type === "wait") return Number.isInteger(step.value) && step.value >= 100 && step.value <= 3000;
  return false;
}

function cleanSteps(steps) {
  if (!Array.isArray(steps)) return [];
  return steps.filter(step => validateStep(step)).slice(0, 120).map(step => ({
    id: String(step.id || ""), type: step.type, target: step.target,
    ...(step.type === "input" ? {field: step.field, value: step.value} : {}),
    ...(step.type === "click" ? {action: step.action, ...(step.action === "mark-ready" ? {key: step.key} : {})} : {}),
    ...(["scroll", "wait"].includes(step.type) ? {value: step.value} : {})
  }));
}

if (typeof module !== "undefined") module.exports = {SAFE_ACTIONS, SAFE_FIELDS, isSensitive, isSensitiveValue, validateStep, cleanSteps};
if (typeof window !== "undefined") window.MacroForge = {SAFE_ACTIONS, SAFE_FIELDS, isSensitive, isSensitiveValue, validateStep, cleanSteps};
