// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState, useEffect, useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { ConfirmDeleteDialog } from '../components/ui/confirm-delete-dialog';
import { RefreshCw } from 'lucide-react';
import {
  useProviders,
  useCreateProvider,
  useUpdateProvider,
  useTestProvider,
  useDeleteProvider,
  useFetchModels,
  useRefreshSavedModels,
} from '../hooks/useProviders';
import { providersApi } from '../api/providers';
import CacheStats from '../components/CacheStats';
import { useAppStore } from '../store/appStore';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { chatApi } from '../api/chat';
import { useStagedSettings } from '../hooks/useStagedSettings';
import { cn } from '@/lib/utils';
import type { ProviderStatus, VerifiedExample } from '../types';
import { Button } from '../components/ui/button';
import { apiErrorMessage } from '../lib/apiError';

type Tab = 'providers' | 'execution' | 'optimization' | 'system' | 'examples';

const TABS: { id: Tab; label: string }[] = [
  { id: 'providers', label: 'LLM Providers' },
  { id: 'execution', label: 'Query Execution' },
  { id: 'optimization', label: 'AI & Optimization' },
  { id: 'system', label: 'System & Security' },
  { id: 'examples', label: 'Examples Library' },
];

const isTab = (value: string | null): value is Tab =>
  TABS.some((t) => t.id === value);

// Only for truly custom/unknown compatible services (Gemini, Groq, Cerebras, Mistral now have dedicated types)
const CUSTOM_SERVICES: { label: string; base_url: string; default_model: string }[] = [
  { label: 'HuggingFace (Free)', base_url: 'https://router.huggingface.co/v1', default_model: 'meta-llama/Llama-3.2-3B-Instruct' },
  { label: 'Together.ai', base_url: 'https://api.together.xyz/v1', default_model: 'meta-llama/Llama-3.3-70B-Instruct-Turbo' },
  { label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', default_model: 'openrouter/free' },
  { label: 'Custom URL', base_url: '', default_model: '' },
];

function ProviderCard({ provider }: { provider: ProviderStatus }) {
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(provider.current_model);
  const [displayName, setDisplayName] = useState(provider.display_name);
  const [editing, setEditing] = useState(false);
  const [dynamicModels, setDynamicModels] = useState<string[]>([]);

  useEffect(() => {
    if (!editing) {
      setModel(provider.current_model);
      setDisplayName(provider.display_name);
    }
  }, [provider.current_model, provider.display_name, editing]);
  const update = useUpdateProvider();
  const test = useTestProvider();
  const del = useDeleteProvider();
  const refreshModels = useRefreshSavedModels();
  const [testResult, setTestResult] = useState<string | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const handleRefreshModels = async () => {
    if (!provider.id) return;
    try {
      const models = await refreshModels.mutateAsync(provider.id);
      setDynamicModels(models);
    } catch {
      // silently ignore — user still has the existing list
    }
  };

  const modelOptions = dynamicModels.length > 0 ? dynamicModels : provider.available_models;

  const handleSave = async () => {
    if (!provider.id) return;
    await update.mutateAsync({
      id: provider.id,
      payload: { api_key: apiKey || undefined, model, display_name: displayName, is_active: true },
    });
    setApiKey('');
    setEditing(false);
  };

  const handleCancel = () => {
    // Closing the editor re-runs the `!editing` effect, which restores displayName and
    // model from the provider. The API key is not derived from it — a typed-but-unsaved
    // key would survive into the next Edit and get submitted, so clear it here.
    setApiKey('');
    setEditing(false);
  };

  const handleTest = async () => {
    if (!provider.id) return;
    setTestResult(null);
    try {
      const r = await test.mutateAsync(provider.id);
      setTestResult(
        r.success
          ? `✓ ${r.message}${r.latency_ms ? ` (${r.latency_ms}ms)` : ''}`
          : `✗ ${r.message}`,
      );
    } catch (err) {
      setTestResult(
        `✗ Network error: ${err instanceof Error ? err.message : 'unable to reach provider'}`,
      );
    }
  };

  return (
    <div className="space-y-3 rounded-xl border border-border bg-card p-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              'h-2.5 w-2.5 rounded-full',
              provider.is_healthy
                ? 'bg-success'
                : provider.is_configured
                  ? 'bg-destructive'
                  : 'bg-muted-foreground',
            )}
          />
          <span className="text-sm font-medium text-foreground">{provider.display_name}</span>
          {provider.base_url && (
            <span className="max-w-xs truncate text-xs text-muted-foreground">
              {provider.base_url}
            </span>
          )}
        </div>
        <div className="flex gap-3">
          <button
            onClick={handleTest}
            disabled={!provider.is_configured || test.isPending}
            className="text-xs text-primary transition-colors hover:opacity-70 disabled:opacity-40"
          >
            {test.isPending ? 'Testing…' : 'Test'}
          </button>
          <button
            onClick={() => setEditing((o) => !o)}
            className="text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            Edit
          </button>
          {provider.id && (
            <button
              onClick={() => setDeleteConfirmOpen(true)}
              className="text-xs text-destructive transition-colors hover:opacity-70"
            >
              Delete
            </button>
          )}
        </div>
      </div>

      <p className="text-xs text-muted-foreground">
        Model: <span className="font-mono">{provider.current_model || '—'}</span>
      </p>

      {testResult && (
        <p
          className={cn(
            'text-xs',
            testResult.startsWith('✓') ? 'text-success' : 'text-destructive',
          )}
        >
          {testResult}
        </p>
      )}

      {editing && (
        <div className="space-y-2 border-t border-border pt-3">
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="Display name"
            autoComplete="off"
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <p className="text-xs text-muted-foreground">A label shown in the model picker — purely cosmetic.</p>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="API key (leave blank to keep existing)"
            autoComplete="new-password"
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <p className="text-xs text-muted-foreground">Stored encrypted. Leave blank to keep the existing key.</p>
          <p className="text-xs text-muted-foreground">The model used to generate SQL for this provider.</p>
          <div className="flex items-center gap-2">
            {modelOptions.length > 0 ? (
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              >
                {modelOptions.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            ) : (
              <input
                value={model}
                onChange={(e) => setModel(e.target.value)}
                placeholder="Model name"
                className="w-full rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              />
            )}
            {provider.id && (
              <button
                type="button"
                onClick={handleRefreshModels}
                disabled={refreshModels.isPending}
                title="Fetch latest models from provider"
                className="flex-shrink-0 rounded-md border border-border p-1.5 text-muted-foreground transition-colors hover:text-foreground disabled:opacity-40"
              >
                <RefreshCw className={cn('h-3.5 w-3.5', refreshModels.isPending && 'animate-spin')} />
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleSave}
              disabled={update.isPending}
              className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {update.isPending ? 'Saving…' : 'Save'}
            </button>
            <button
              type="button"
              onClick={handleCancel}
              disabled={update.isPending}
              className="rounded-md border border-border px-4 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <ConfirmDeleteDialog
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        title="Delete provider?"
        description={
          <>
            This will permanently remove{' '}
            <strong>{provider.display_name}</strong> and its API key. This
            cannot be undone.
          </>
        }
        onConfirm={() => {
          del.mutate(provider.id!);
          setDeleteConfirmOpen(false);
        }}
        isPending={del.isPending}
      />
    </div>
  );
}

// Inline form for adding a new config row to any named provider type.
// Handles: api-key providers with model dropdown, ollama (no key, needs base_url).
function AddProviderInlineForm({
  providerType,
  displayNameHint,
  availableModels,
  onClose,
}: {
  providerType: string;
  displayNameHint: string;
  availableModels: string[];
  onClose: () => void;
}) {
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(availableModels[0] ?? '');
  const [baseUrl, setBaseUrl] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [dynamicModels, setDynamicModels] = useState<string[]>([]);
  const [fetchModelsError, setFetchModelsError] = useState<string | null>(null);
  const create = useCreateProvider();
  const fetchModelsMutation = useFetchModels();
  const [success, setSuccess] = useState(false);
  const [isTesting, setIsTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  const isOllama = providerType === 'ollama';
  const modelList = dynamicModels.length > 0 ? dynamicModels : availableModels;

  const handleFetchModels = async () => {
    setFetchModelsError(null);
    try {
      const models = await fetchModelsMutation.mutateAsync({
        provider_type: providerType,
        api_key: isOllama ? undefined : apiKey || undefined,
        base_url: isOllama ? baseUrl || undefined : undefined,
      });
      if (models.length === 0) {
        setFetchModelsError('No models returned — check your API key');
      } else {
        setDynamicModels(models);
        setModel(models[0]);
      }
    } catch (e: unknown) {
      setFetchModelsError(apiErrorMessage(e, 'Failed to fetch models'));
    }
  };

  const canFetchModels = isOllama ? true : apiKey.length > 5;

  const handleTest = async () => {
    setIsTesting(true);
    setTestResult(null);
    try {
      const data = await providersApi.testNew({
        provider_type: providerType,
        api_key: isOllama ? undefined : apiKey || undefined,
        model: model || undefined,
        base_url: isOllama ? baseUrl || undefined : undefined,
      });
      setTestResult(data);
    } catch (e: unknown) {
      setTestResult({ success: false, message: apiErrorMessage(e, 'Test failed') });
    } finally {
      setIsTesting(false);
    }
  };

  const handleAdd = async () => {
    try {
      await create.mutateAsync({
        provider_type: providerType,
        display_name: displayName || displayNameHint,
        api_key: isOllama ? undefined : apiKey || undefined,
        base_url: isOllama ? baseUrl || undefined : undefined,
        model,
        is_active: true,
      });
      setSuccess(true);
      setTimeout(() => {
        setSuccess(false);
        onClose();
      }, 1500);
    } catch {
      // error surfaced via create.error
    }
  };

  return (
    <div className="space-y-3 rounded-xl border border-dashed border-border bg-card/50 p-4">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-muted-foreground">Display name</label>
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={displayNameHint}
            autoComplete="off"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <p className="mt-1 text-xs text-muted-foreground">A label shown in the model picker — purely cosmetic.</p>
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Model</label>
          {modelList.length > 0 ? (
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {modelList.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          ) : (
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="e.g. llama3"
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
          )}
          <p className="mt-1 text-xs text-muted-foreground">The model used to generate SQL for this provider.</p>
        </div>
        {isOllama && (
          <div className="col-span-2">
            <label className="text-xs text-muted-foreground">Base URL</label>
            <input
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://localhost:11434"
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <p className="mt-1 text-xs text-muted-foreground">The provider's API endpoint.</p>
          </div>
        )}
        {!isOllama && (
          <div className="col-span-2">
            <label className="text-xs text-muted-foreground">API Key</label>
            <div className="mt-1 flex gap-2">
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                autoComplete="new-password"
                className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              />
              <button
                type="button"
                onClick={handleFetchModels}
                disabled={!canFetchModels || fetchModelsMutation.isPending}
                className="flex-shrink-0 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary disabled:opacity-40"
              >
                {fetchModelsMutation.isPending ? (
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  'Fetch Models'
                )}
              </button>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">Stored encrypted; never displayed after saving.</p>
          </div>
        )}
        {isOllama && (
          <div className="col-span-2 flex justify-end">
            <button
              type="button"
              onClick={handleFetchModels}
              disabled={fetchModelsMutation.isPending}
              className="rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary disabled:opacity-40"
            >
              {fetchModelsMutation.isPending ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                'Fetch Models'
              )}
            </button>
          </div>
        )}
      </div>
      {fetchModelsError && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <span>✗</span>
          <span className="break-all">{fetchModelsError}</span>
        </div>
      )}
      {testResult && (
        <div
          className={cn(
            'flex items-start gap-2 rounded-lg border px-3 py-2 text-xs',
            testResult.success
              ? 'border-success/30 bg-success/10 text-success'
              : 'border-destructive/30 bg-destructive/10 text-destructive',
          )}
        >
          <span>{testResult.success ? '✓' : '✗'}</span>
          <span className="break-all">{testResult.message}</span>
        </div>
      )}
      {create.isError && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <span>✗</span>
          <span className="break-all">
            {apiErrorMessage(create.error, 'Failed to save provider')}
          </span>
        </div>
      )}
      <div className="flex items-center gap-3">
        <Button
          variant="default"
          onClick={handleTest}
          disabled={isTesting || (!isOllama && !apiKey)}
        >
          {isTesting ? 'Testing…' : 'Test'}
        </Button>
        <button
          onClick={handleAdd}
          disabled={create.isPending || (!isOllama && !apiKey)}
          className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {create.isPending ? 'Adding…' : 'Add'}
        </button>
        <button
          onClick={onClose}
          className="text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          Cancel
        </button>
        {success && <span className="text-xs text-success">✓ Added</span>}
      </div>
    </div>
  );
}

// A single provider type section: header + all saved configs + Add button
function ProviderSection({
  providerType,
  displayName,
  availableModels,
  configs,
}: {
  providerType: string;
  displayName: string;
  availableModels: string[];
  configs: ProviderStatus[];
}) {
  const [adding, setAdding] = useState(false);
  const savedConfigs = configs.filter((p) => p.id !== null);

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {displayName}
      </h3>
      {savedConfigs.map((p) => (
        <ProviderCard key={p.id} provider={p} />
      ))}
      {adding ? (
        <AddProviderInlineForm
          providerType={providerType}
          displayNameHint={displayName}
          availableModels={availableModels}
          onClose={() => setAdding(false)}
        />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="w-full rounded-xl border border-dashed border-border py-2 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary"
        >
          + Add {displayName} config
        </button>
      )}
    </div>
  );
}

// Custom OpenAI-compatible provider form (HuggingFace, Together, OpenRouter, custom URL)
function AddCustomProvider({ onClose }: { onClose: () => void }) {
  const [service, setService] = useState(CUSTOM_SERVICES[0]);
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(service.default_model);
  const [baseUrl, setBaseUrl] = useState(service.base_url);
  const [displayName, setDisplayName] = useState(service.label);
  const [dynamicModels, setDynamicModels] = useState<string[]>([]);
  const [fetchModelsError, setFetchModelsError] = useState<string | null>(null);
  const create = useCreateProvider();
  const fetchModelsMutation = useFetchModels();
  const [isTesting, setIsTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null);

  const canFetchModels = apiKey.length > 5 && baseUrl.length > 0;

  const handleFetchModels = async () => {
    setFetchModelsError(null);
    try {
      const models = await fetchModelsMutation.mutateAsync({
        provider_type: 'openai_compatible',
        api_key: apiKey,
        base_url: baseUrl,
      });
      if (models.length === 0) {
        setFetchModelsError('No models returned — check your API key and base URL');
      } else {
        setDynamicModels(models);
        setModel(models[0]);
      }
    } catch (e: unknown) {
      setFetchModelsError(apiErrorMessage(e, 'Failed to fetch models'));
    }
  };

  const handleTest = async () => {
    setIsTesting(true);
    setTestResult(null);
    try {
      const data = await providersApi.testNew({
        provider_type: 'openai_compatible',
        api_key: apiKey || undefined,
        model: model || undefined,
        base_url: baseUrl || undefined,
      });
      setTestResult(data);
    } catch (e: unknown) {
      setTestResult({ success: false, message: apiErrorMessage(e, 'Test failed') });
    } finally {
      setIsTesting(false);
    }
  };

  const handleServiceChange = (label: string) => {
    const svc = CUSTOM_SERVICES.find((s) => s.label === label) ?? CUSTOM_SERVICES[0];
    setService(svc);
    setModel(svc.default_model);
    setBaseUrl(svc.base_url);
    setDisplayName(svc.label);
    setDynamicModels([]);
    setFetchModelsError(null);
  };

  const handleAdd = async () => {
    try {
      await create.mutateAsync({
        provider_type: 'openai_compatible',
        display_name: displayName,
        base_url: baseUrl,
        api_key: apiKey,
        model,
        is_active: true,
      });
      setApiKey('');
      onClose();
    } catch {
      // error surfaced via create.error
    }
  };

  return (
    <div className="space-y-3 rounded-xl border-2 border-dashed border-border bg-card p-4">
      <h3 className="text-sm font-semibold text-foreground">+ Add Custom Provider</h3>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-muted-foreground">Service</label>
          <select
            value={service.label}
            onChange={(e) => handleServiceChange(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          >
            {CUSTOM_SERVICES.map((s) => (
              <option key={s.label} value={s.label}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Display name</label>
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            autoComplete="off"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Base URL</label>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            autoComplete="off"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <p className="mt-1 text-xs text-muted-foreground">The provider's API endpoint.</p>
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Model</label>
          {dynamicModels.length > 0 ? (
            <select
              value={model}
              onChange={(e) => setModel(e.target.value)}
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              {dynamicModels.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          ) : (
            <input
              value={model}
              onChange={(e) => setModel(e.target.value)}
              autoComplete="off"
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
          )}
          <p className="mt-1 text-xs text-muted-foreground">The model used to generate SQL for this provider.</p>
        </div>
        <div className="col-span-2">
          <label className="text-xs text-muted-foreground">API Key</label>
          <div className="mt-1 flex gap-2">
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              autoComplete="new-password"
              className="w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <button
              type="button"
              onClick={handleFetchModels}
              disabled={!canFetchModels || fetchModelsMutation.isPending}
              className="flex-shrink-0 rounded-md border border-border px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary disabled:opacity-40"
            >
              {fetchModelsMutation.isPending ? (
                <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              ) : (
                'Fetch Models'
              )}
            </button>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">Stored encrypted; never displayed after saving.</p>
        </div>
      </div>
      {fetchModelsError && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <span>✗</span>
          <span className="break-all">{fetchModelsError}</span>
        </div>
      )}
      {create.isError && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
          <span>✗</span>
          <span className="break-all">
            {apiErrorMessage(create.error, 'Failed to save provider')}
          </span>
        </div>
      )}
      {testResult && (
        <div
          className={cn(
            'flex items-start gap-2 rounded-lg border px-3 py-2 text-xs',
            testResult.success
              ? 'border-success/30 bg-success/10 text-success'
              : 'border-destructive/30 bg-destructive/10 text-destructive',
          )}
        >
          <span>{testResult.success ? '✓' : '✗'}</span>
          <span className="break-all">{testResult.message}</span>
        </div>
      )}
      <div className="flex items-center gap-3">
        <Button
          variant="default"
          onClick={handleTest}
          disabled={isTesting || !apiKey || !baseUrl}
        >
          {isTesting ? 'Testing…' : 'Test'}
        </Button>
        <button
          onClick={handleAdd}
          disabled={create.isPending || !apiKey}
          className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {create.isPending ? 'Adding…' : 'Add Provider'}
        </button>
        <button
          onClick={onClose}
          className="text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

interface SaveRowProps {
  onSave: () => void;
  dirty: boolean;
  update: { isPending: boolean; isSuccess: boolean; isError: boolean; error: unknown };
}

/** Save button plus outcome for a staged-settings form. Shared so no tab can forget the error row. */
function SaveRow({ onSave, dirty, update }: SaveRowProps) {
  return (
    <div className="flex items-center gap-3">
      <button onClick={onSave} disabled={update.isPending || !dirty} className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50">
        {update.isPending ? 'Saving…' : 'Save'}
      </button>
      {/* isSuccess stays true after the first save, so gate the badge on the form being clean. */}
      {update.isSuccess && !dirty && <span className="text-xs text-success">✓ Saved</span>}
      {update.isError && (
        <span className="text-xs text-destructive">
          {apiErrorMessage(update.error, 'Failed to save')}
        </span>
      )}
    </div>
  );
}

const SettingsSkeleton = () => <div className="h-24 animate-pulse rounded-lg bg-muted" />;

const EXECUTION_KEYS = ['default_query_timeout', 'default_row_limit'] as const;

function QueryExecutionTab() {
  const { values, set, dirty, save, update } = useStagedSettings(EXECUTION_KEYS);
  if (!values) return <SettingsSkeleton />;

  return (
    <div className="space-y-6">
      <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        These limits guard against runaway queries. Settings are saved and applied
        immediately — they take effect on the next query.
      </p>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Query Timeout</label>
          <span className="text-sm text-muted-foreground">{values.default_query_timeout}s</span>
        </div>
        <input type="range" min={5} max={300} step={5} value={values.default_query_timeout} onChange={(e) => set('default_query_timeout', Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Cancel queries that run longer than this many seconds.</p>
      </div>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Row Limit</label>
          <span className="text-sm text-muted-foreground">{values.default_row_limit.toLocaleString()} rows</span>
        </div>
        <input type="range" min={100} max={10000} step={100} value={values.default_row_limit} onChange={(e) => set('default_row_limit', Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Truncate result sets larger than this many rows.</p>
      </div>
      <SaveRow onSave={save} dirty={dirty} update={update} />
    </div>
  );
}

const SYSTEM_KEYS = ['db_pool_size', 'db_max_overflow', 'bcrypt_rounds'] as const;

function SystemSecurityTab() {
  const { values, set, dirty, save, update } = useStagedSettings(SYSTEM_KEYS);
  if (!values) return <SettingsSkeleton />;

  return (
    <div className="space-y-6">
      <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        Deployment-level knobs. Settings are saved immediately but take effect after the next
        backend restart — except the bcrypt work factor, which applies on the next password operation.
      </p>
      <div>
        <h3 className="mb-1 text-sm font-semibold text-foreground">Database Connection Pool</h3>
        <p className="mb-4 text-xs text-muted-foreground">Tunes how many connections the app keeps open to its own database. Changes take effect on the next process restart.</p>
        <div className="space-y-4">
          <div>
            <div className="mb-1 flex justify-between">
              <label className="text-sm font-medium text-foreground">Pool Size</label>
              <span className="text-sm text-muted-foreground">{values.db_pool_size}</span>
            </div>
            <input type="range" min={1} max={50} step={1} value={values.db_pool_size} onChange={(e) => set('db_pool_size', Number(e.target.value))} className="w-full accent-primary" />
            <p className="mt-1 text-xs text-muted-foreground">Persistent connections kept open. Increase for higher concurrency.</p>
          </div>
          <div>
            <div className="mb-1 flex justify-between">
              <label className="text-sm font-medium text-foreground">Max Overflow</label>
              <span className="text-sm text-muted-foreground">{values.db_max_overflow}</span>
            </div>
            <input type="range" min={0} max={100} step={5} value={values.db_max_overflow} onChange={(e) => set('db_max_overflow', Number(e.target.value))} className="w-full accent-primary" />
            <p className="mt-1 text-xs text-muted-foreground">Extra connections allowed above pool size during traffic spikes.</p>
          </div>
        </div>
      </div>
      <div className="border-t border-border pt-6">
        <h3 className="mb-4 text-sm font-semibold text-foreground">Security</h3>
        <div>
          <div className="mb-1 flex justify-between">
            <label className="text-sm font-medium text-foreground">bcrypt Work Factor</label>
            <span className="text-sm text-muted-foreground">{values.bcrypt_rounds}</span>
          </div>
          <input type="range" min={10} max={16} step={1} value={values.bcrypt_rounds} onChange={(e) => set('bcrypt_rounds', Number(e.target.value))} className="w-full accent-primary" />
          <p className="mt-1 text-xs text-muted-foreground">Higher values slow down login slightly but make brute-force harder. Takes effect on the next password operation.</p>
        </div>
      </div>
      <SaveRow onSave={save} dirty={dirty} update={update} />
    </div>
  );
}

const OPTIMIZATION_KEYS = [
  'cache_enabled',
  'semantic_similarity_threshold',
  'cache_max_age_days',
  'schema_pruning_enabled',
  'schema_pruning_top_k',
] as const;

function OptimizationSettingsSection() {
  // Every control in this section is staged locally and written only by Save — a toggle that
  // wrote on click saved half the form behind the user's back, and left the other sliders
  // looking saved when they were not.
  const { values, set, dirty, save, update } = useStagedSettings(OPTIMIZATION_KEYS);
  if (!values) return <SettingsSkeleton />;

  return (
    <div className="mb-4 space-y-4 border-b border-border pb-4">
      <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
        These settings reduce LLM cost and latency — the cache reuses past answers, schema
        pruning shrinks the prompt. Changes apply as soon as you press Save.
      </p>
      <h3 className="text-sm font-semibold text-foreground">Query Cache</h3>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">Enable Query Cache</p>
          <p className="text-xs text-muted-foreground">Cache semantically similar queries to skip LLM calls.</p>
        </div>
        <button
          onClick={() => set('cache_enabled', !values.cache_enabled)}
          className={cn(
            'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
            values.cache_enabled ? 'bg-primary' : 'bg-muted-foreground',
          )}
        >
          <span className={cn('inline-block h-4 w-4 transform rounded-full bg-primary-foreground transition-transform', values.cache_enabled ? 'translate-x-6' : 'translate-x-1')} />
        </button>
      </div>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Similarity Threshold</label>
          <span className="text-sm text-muted-foreground">{values.semantic_similarity_threshold.toFixed(2)}</span>
        </div>
        <input type="range" min={0.5} max={1.0} step={0.01} value={values.semantic_similarity_threshold} onChange={(e) => set('semantic_similarity_threshold', Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Higher values require closer matches before serving a cached result.</p>
      </div>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Cache Max Age</label>
          <span className="text-sm text-muted-foreground">{values.cache_max_age_days} days</span>
        </div>
        <input type="range" min={1} max={365} step={1} value={values.cache_max_age_days} onChange={(e) => set('cache_max_age_days', Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Cache entries older than this are automatically discarded.</p>
      </div>
      <div className="border-t border-border pt-4">
        <h3 className="mb-4 text-sm font-semibold text-foreground">Schema Pruning</h3>
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-foreground">Enable Schema Pruning</p>
              <p className="text-xs text-muted-foreground">Filter schema context to the most relevant tables before each LLM call — reduces token usage significantly.</p>
            </div>
            <button
              onClick={() => set('schema_pruning_enabled', !values.schema_pruning_enabled)}
              className={cn(
                'relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors',
                values.schema_pruning_enabled ? 'bg-primary' : 'bg-muted-foreground',
              )}
            >
              <span className={cn('inline-block h-4 w-4 transform rounded-full bg-primary-foreground transition-transform', values.schema_pruning_enabled ? 'translate-x-6' : 'translate-x-1')} />
            </button>
          </div>
          <div>
            <div className="mb-1 flex justify-between">
              <label className="text-sm font-medium text-foreground">Max Tables (Top K)</label>
              <span className="text-sm text-muted-foreground">{values.schema_pruning_top_k}</span>
            </div>
            <input type="range" min={3} max={40} step={1} value={values.schema_pruning_top_k} onChange={(e) => set('schema_pruning_top_k', Number(e.target.value))} className="w-full accent-primary" />
            <p className="mt-1 text-xs text-muted-foreground">Maximum number of tables passed to the LLM. Lower values reduce token usage; increase if queries span many tables.</p>
          </div>
        </div>
      </div>
      <SaveRow onSave={save} dirty={dirty} update={update} />
    </div>
  );
}

function ExampleCard({
  ex,
  connectionId,
}: {
  ex: VerifiedExample;
  connectionId: string;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [editQuestion, setEditQuestion] = useState(ex.question);
  const [editQuery, setEditQuery] = useState(ex.query);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const deleteExample = useMutation({
    mutationFn: chatApi.deleteExample,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['examples', connectionId] }),
  });
  const updateExample = useMutation({
    mutationFn: () => chatApi.updateExample(ex.id, editQuestion, editQuery),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['examples', connectionId] });
      setEditing(false);
    },
  });

  const handleCancel = () => {
    setEditQuestion(ex.question);
    setEditQuery(ex.query);
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="space-y-2 rounded-xl border border-ring bg-card p-3">
        <input
          value={editQuestion}
          onChange={(e) => setEditQuestion(e.target.value)}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <textarea
          value={editQuery}
          onChange={(e) => setEditQuery(e.target.value)}
          rows={3}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="flex gap-2">
          <button
            onClick={() => updateExample.mutate()}
            disabled={!editQuestion || !editQuery || updateExample.isPending}
            className="rounded-md bg-brand-gradient px-3 py-1 text-xs font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            {updateExample.isPending ? 'Saving…' : 'Save'}
          </button>
          <button
            onClick={handleCancel}
            className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-1 rounded-xl border border-border bg-card p-3">
      <div className="flex items-start justify-between">
        <p className="text-sm text-foreground">{ex.question}</p>
        <div className="ml-2 flex shrink-0 gap-3">
          <button
            onClick={() => setEditing(true)}
            className="text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            Edit
          </button>
          <button
            onClick={() => setDeleteConfirmOpen(true)}
            className="text-xs text-destructive transition-colors hover:opacity-70"
          >
            Delete
          </button>
        </div>
      </div>
      <pre className="overflow-x-auto rounded bg-muted px-2 py-1 font-mono text-xs text-muted-foreground">
        {ex.query}
      </pre>

      <ConfirmDeleteDialog
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        title="Delete example?"
        description="This verified example will be permanently removed."
        onConfirm={() => {
          deleteExample.mutate(ex.id);
          setDeleteConfirmOpen(false);
        }}
        isPending={deleteExample.isPending}
      />
    </div>
  );
}

function ExamplesTab({ connectionId }: { connectionId: string }) {
  const queryClient = useQueryClient();
  const { data: examples, isLoading } = useQuery({
    queryKey: ['examples', connectionId],
    queryFn: () => chatApi.getExamples(connectionId),
    enabled: !!connectionId,
  });
  const [question, setQuestion] = useState('');
  const [query, setQuery] = useState('');
  const addExample = useMutation({
    mutationFn: () => chatApi.addExample(connectionId, question, query),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['examples', connectionId] });
      setQuestion('');
      setQuery('');
    },
  });

  if (!connectionId)
    return (
      <p className="text-sm text-muted-foreground">
        Select a connection first.{' '}
        <a href="/connect" className="text-primary underline hover:opacity-80">
          Add a connection
        </a>
      </p>
    );

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold text-foreground">Verified Examples Library</h3>
      <p className="text-xs text-muted-foreground">
        Verified examples teach the model your preferred query patterns. For each new question,
        the most similar examples are retrieved into the prompt, improving accuracy for your schema.
      </p>
      {isLoading ? (
        <div className="h-16 animate-pulse rounded-lg bg-muted" />
      ) : (
        <div className="space-y-2">
          {examples?.map((ex) => (
            <ExampleCard key={ex.id} ex={ex} connectionId={connectionId} />
          ))}
        </div>
      )}
      <div className="space-y-2 border-t border-border pt-4">
        <h4 className="text-sm font-medium text-foreground">Add example manually</h4>
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Natural language question"
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <p className="text-xs text-muted-foreground">The natural-language question a user might ask.</p>
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="SQL query"
          rows={3}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <p className="text-xs text-muted-foreground">The correct SQL that answers the question.</p>
        <button
          onClick={() => addExample.mutate()}
          disabled={!question || !query || addExample.isPending}
          className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          Add Example
        </button>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  // The tab lives in the URL, not in local state, so a refresh or a shared link lands on the
  // section the user was reading. An unknown or absent ?tab= falls back to the first tab.
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab');
  const activeTab: Tab = isTab(tabParam) ? tabParam : 'providers';
  const setActiveTab = (tab: Tab) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', tab);
    // replace, so switching tabs does not bury the previous page under history entries.
    setSearchParams(next, { replace: true });
  };
  const [addingCustom, setAddingCustom] = useState(false);
  const { data: providers } = useProviders();
  const { activeConnectionId } = useAppStore();

  // Group providers by type, preserving the API's ordering (configured first).
  // openai_compatible is separated out so it renders with AddCustomProvider.
  const { namedGroups, customConfigs } = useMemo(() => {
    const map = new Map<
      string,
      { displayName: string; availableModels: string[]; configs: ProviderStatus[] }
    >();
    const custom: ProviderStatus[] = [];

    for (const p of providers ?? []) {
      if (p.provider_type === 'openai_compatible') {
        if (p.id) custom.push(p);
        continue;
      }
      if (!map.has(p.provider_type)) {
        map.set(p.provider_type, {
          displayName: p.provider_display_name,
          availableModels: p.available_models,
          configs: [],
        });
      }
      map.get(p.provider_type)!.configs.push(p);
    }

    return {
      namedGroups: Array.from(map.entries()).map(([type, data]) => ({ type, ...data })),
      customConfigs: custom,
    };
  }, [providers]);

  return (
    <div className="flex-1 overflow-auto bg-background">
      <div className="mx-auto max-w-3xl px-4 py-8">
        <h1 className="mb-6 font-display text-2xl font-bold text-foreground">Settings</h1>

        {/* Tabs */}
        <div role="tablist" aria-label="Settings sections" className="mb-6 flex gap-1 border-b border-border">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`settings-tabpanel-${tab.id}`}
              id={`settings-tab-${tab.id}`}
              onClick={() => setActiveTab(tab.id)}
              className={cn(
                '-mb-px border-b-2 px-4 py-2 text-sm font-medium transition-colors',
                activeTab === tab.id
                  ? 'border-primary text-primary'
                  : 'border-transparent text-muted-foreground hover:text-foreground',
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>

        <div
          role="tabpanel"
          id={`settings-tabpanel-${activeTab}`}
          aria-labelledby={`settings-tab-${activeTab}`}
        >
          {activeTab === 'providers' && (
            <div className="space-y-8">
              <p className="rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                Configure the LLM providers that translate natural language into SQL. The colored dot
                shows each provider's last health check: green = healthy, red = configured but failing,
                grey = not configured.
              </p>
              {namedGroups.map((group) => (
                <ProviderSection
                  key={group.type}
                  providerType={group.type}
                  displayName={group.displayName}
                  availableModels={group.availableModels}
                  configs={group.configs}
                />
              ))}

              {/* Custom / OpenAI-compatible section */}
              <div className="space-y-2">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  Custom Providers
                </h3>
                {customConfigs.map((p) => (
                  <ProviderCard key={p.id} provider={p} />
                ))}
                {addingCustom ? (
                  <AddCustomProvider onClose={() => setAddingCustom(false)} />
                ) : (
                  <button
                    onClick={() => setAddingCustom(true)}
                    className="w-full rounded-xl border border-dashed border-border py-2 text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary"
                  >
                    + Add Custom Provider
                  </button>
                )}
              </div>
            </div>
          )}

          {activeTab === 'execution' && <QueryExecutionTab />}

          {activeTab === 'system' && <SystemSecurityTab />}

          {activeTab === 'optimization' && (
            <div>
              <OptimizationSettingsSection />
              {activeConnectionId ? (
                <CacheStats connectionId={activeConnectionId} />
              ) : (
                <p className="text-sm text-muted-foreground">
                  Select an active connection to view cache stats.{' '}
                  <a href="/connect" className="text-primary underline hover:opacity-80">
                    Add a connection
                  </a>
                </p>
              )}
            </div>
          )}

          {activeTab === 'examples' && (
            <ExamplesTab connectionId={activeConnectionId ?? ''} />
          )}
        </div>
      </div>
    </div>
  );
}
