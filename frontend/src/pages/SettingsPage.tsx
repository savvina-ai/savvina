// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState, useEffect, useMemo } from 'react';
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
import { settingsApi } from '../api/settings';
import { cn } from '@/lib/utils';
import type { ProviderStatus, VerifiedExample } from '../types';
import { Button } from '../components/ui/button';

type Tab = 'providers' | 'safety' | 'cache' | 'examples';

// Only for truly custom/unknown compatible services (Gemini, Groq, Cerebras, Mistral now have dedicated types)
const CUSTOM_SERVICES: { label: string; base_url: string; default_model: string }[] = [
  { label: 'GitHub Models (Free)', base_url: 'https://models.inference.ai.azure.com', default_model: 'gpt-4o-mini' },
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
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="API key (leave blank to keep existing)"
            className="w-full rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
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
          <button
            onClick={handleSave}
            disabled={update.isPending}
            className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          >
            Save
          </button>
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
  envConfigured = false,
  defaultModel,
  onClose,
}: {
  providerType: string;
  displayNameHint: string;
  availableModels: string[];
  envConfigured?: boolean;
  defaultModel?: string;
  onClose: () => void;
}) {
  const [apiKey, setApiKey] = useState('');
  const [model, setModel] = useState(defaultModel ?? availableModels[0] ?? '');
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
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setFetchModelsError(detail ?? 'Failed to fetch models');
    }
  };

  const canFetchModels = isOllama ? true : apiKey.length > 5 || envConfigured;

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
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setTestResult({ success: false, message: detail ?? 'Test failed' });
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
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
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
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
          )}
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
          </div>
        )}
        {!isOllama && (
          <div className="col-span-2">
            <label className="text-xs text-muted-foreground">
              API Key
              {envConfigured && (
                <span className="ml-2 text-success">✓ env key configured — leave blank to use it</span>
              )}
            </label>
            <div className="mt-1 flex gap-2">
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={envConfigured ? 'Leave blank to use env key' : ''}
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
            {(create.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to save provider'}
          </span>
        </div>
      )}
      <div className="flex items-center gap-3">
        <Button
          variant="default"
          onClick={handleTest}
          disabled={isTesting || (!isOllama && !apiKey && !envConfigured)}
        >
          {isTesting ? 'Testing…' : 'Test'}
        </Button>
        <button
          onClick={handleAdd}
          disabled={create.isPending || (!isOllama && !apiKey && !envConfigured)}
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
  const envConfig = configs.find((p) => p.id === null && p.is_configured);

  return (
    <div className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {displayName}
      </h3>
      {savedConfigs.map((p) => (
        <ProviderCard key={p.id} provider={p} />
      ))}
      {envConfig && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <span className="h-2 w-2 rounded-full bg-success shrink-0" />
          <span>
            Configured via environment variable · default model:{' '}
            <span className="font-mono text-foreground">{envConfig.current_model || '—'}</span>
          </span>
        </div>
      )}
      {adding ? (
        <AddProviderInlineForm
          providerType={providerType}
          displayNameHint={displayName}
          availableModels={availableModels}
          envConfigured={!!envConfig}
          defaultModel={envConfig?.current_model}
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

// Custom OpenAI-compatible provider form (GitHub Models, HuggingFace, Together, OpenRouter, custom URL)
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
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setFetchModelsError(detail ?? 'Failed to fetch models');
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
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setTestResult({ success: false, message: detail ?? 'Test failed' });
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
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground">Base URL</label>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
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
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 font-mono text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
          )}
        </div>
        <div className="col-span-2">
          <label className="text-xs text-muted-foreground">API Key</label>
          <div className="mt-1 flex gap-2">
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
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
            {(create.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to save provider'}
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

function QuerySafetyTab() {
  const queryClient = useQueryClient();
  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  });
  const update = useMutation({
    mutationFn: settingsApi.update,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['settings'] }),
  });

  const [queryTimeout, setQueryTimeout] = useState<number | null>(null);
  const [rowLimit, setRowLimit] = useState<number | null>(null);
  const [poolSize, setPoolSize] = useState<number | null>(null);
  const [maxOverflow, setMaxOverflow] = useState<number | null>(null);
  const [bcryptRounds, setBcryptRounds] = useState<number | null>(null);

  useEffect(() => {
    if (!settings) return;
    // One-time initialise from server: `?? value` means "set only if not yet touched by user"
    setQueryTimeout((t) => t ?? settings.default_query_timeout);
    setRowLimit((r) => r ?? settings.default_row_limit);
    setPoolSize((p) => p ?? settings.db_pool_size);
    setMaxOverflow((m) => m ?? settings.db_max_overflow);
    setBcryptRounds((b) => b ?? settings.bcrypt_rounds);
  }, [settings]);

  const handleSave = () => {
    if (queryTimeout === null || rowLimit === null || poolSize === null || maxOverflow === null || bcryptRounds === null) return;
    update.mutate({
      default_query_timeout: queryTimeout,
      default_row_limit: rowLimit,
      db_pool_size: poolSize,
      db_max_overflow: maxOverflow,
      bcrypt_rounds: bcryptRounds,
    });
  };

  if (isLoading || queryTimeout === null || rowLimit === null || poolSize === null || maxOverflow === null || bcryptRounds === null) {
    return <div className="h-24 animate-pulse rounded-lg bg-muted" />;
  }

  return (
    <div className="space-y-6">
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Query Timeout</label>
          <span className="text-sm text-muted-foreground">{queryTimeout}s</span>
        </div>
        <input type="range" min={5} max={300} step={5} value={queryTimeout} onChange={(e) => setQueryTimeout(Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Cancel queries that run longer than this many seconds.</p>
      </div>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Row Limit</label>
          <span className="text-sm text-muted-foreground">{rowLimit.toLocaleString()} rows</span>
        </div>
        <input type="range" min={100} max={10000} step={100} value={rowLimit} onChange={(e) => setRowLimit(Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Truncate result sets larger than this many rows.</p>
      </div>
      <div className="border-t border-border pt-6">
        <h3 className="mb-4 text-sm font-semibold text-foreground">Database Connection Pool</h3>
        <p className="mb-4 text-xs text-muted-foreground">Changes to pool settings take effect on the next process restart.</p>
        <div className="space-y-4">
          <div>
            <div className="mb-1 flex justify-between">
              <label className="text-sm font-medium text-foreground">Pool Size</label>
              <span className="text-sm text-muted-foreground">{poolSize}</span>
            </div>
            <input type="range" min={1} max={50} step={1} value={poolSize} onChange={(e) => setPoolSize(Number(e.target.value))} className="w-full accent-primary" />
            <p className="mt-1 text-xs text-muted-foreground">Persistent connections kept open. Increase for higher concurrency.</p>
          </div>
          <div>
            <div className="mb-1 flex justify-between">
              <label className="text-sm font-medium text-foreground">Max Overflow</label>
              <span className="text-sm text-muted-foreground">{maxOverflow}</span>
            </div>
            <input type="range" min={0} max={100} step={5} value={maxOverflow} onChange={(e) => setMaxOverflow(Number(e.target.value))} className="w-full accent-primary" />
            <p className="mt-1 text-xs text-muted-foreground">Extra connections allowed above pool size during traffic spikes.</p>
          </div>
        </div>
      </div>
      <div className="border-t border-border pt-6">
        <h3 className="mb-4 text-sm font-semibold text-foreground">Security</h3>
        <div>
          <div className="mb-1 flex justify-between">
            <label className="text-sm font-medium text-foreground">bcrypt Work Factor</label>
            <span className="text-sm text-muted-foreground">{bcryptRounds}</span>
          </div>
          <input type="range" min={10} max={16} step={1} value={bcryptRounds} onChange={(e) => setBcryptRounds(Number(e.target.value))} className="w-full accent-primary" />
          <p className="mt-1 text-xs text-muted-foreground">Higher values slow down login slightly but make brute-force harder. Takes effect on the next password operation.</p>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <button onClick={handleSave} disabled={update.isPending} className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50">
          {update.isPending ? 'Saving…' : 'Save'}
        </button>
        {update.isSuccess && <span className="text-xs text-success">✓ Saved</span>}
      </div>
    </div>
  );
}

function CacheSettingsSection() {
  const queryClient = useQueryClient();
  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  });
  const update = useMutation({
    mutationFn: settingsApi.update,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['settings'] }),
  });

  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [threshold, setThreshold] = useState<number | null>(null);
  const [maxAgeDays, setMaxAgeDays] = useState<number | null>(null);
  const [schemaPruningEnabled, setSchemaPruningEnabled] = useState<boolean | null>(null);
  const [schemaPruningTopK, setSchemaPruningTopK] = useState<number | null>(null);

  useEffect(() => {
    if (!settings) return;
    // One-time initialise from server: `?? value` means "set only if not yet touched by user"
    setEnabled((e) => e ?? settings.cache_enabled);
    setThreshold((t) => t ?? settings.semantic_similarity_threshold);
    setMaxAgeDays((d) => d ?? settings.cache_max_age_days);
    setSchemaPruningEnabled((p) => p ?? settings.schema_pruning_enabled);
    setSchemaPruningTopK((k) => k ?? settings.schema_pruning_top_k);
  }, [settings]);

  const handleSave = () => {
    if (enabled === null || threshold === null || maxAgeDays === null || schemaPruningTopK === null) return;
    update.mutate({
      cache_enabled: enabled,
      semantic_similarity_threshold: threshold,
      cache_max_age_days: maxAgeDays,
      schema_pruning_top_k: schemaPruningTopK,
    });
  };

  if (isLoading || enabled === null || threshold === null || maxAgeDays === null || schemaPruningEnabled === null || schemaPruningTopK === null) {
    return <div className="h-16 animate-pulse rounded-lg bg-muted" />;
  }

  return (
    <div className="mb-4 space-y-4 border-b border-border pb-4">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-foreground">Enable Query Cache</p>
          <p className="text-xs text-muted-foreground">Cache semantically similar queries to skip LLM calls.</p>
        </div>
        <button
          onClick={() => {
            const next = !enabled;
            setEnabled(next);
            update.mutate(
              { cache_enabled: next, semantic_similarity_threshold: threshold! },
              { onError: () => setEnabled(!next) },
            );
          }}
          className={cn(
            'relative inline-flex h-6 w-11 items-center rounded-full transition-colors',
            enabled ? 'bg-primary' : 'bg-muted-foreground',
          )}
        >
          <span className={cn('inline-block h-4 w-4 transform rounded-full bg-primary-foreground transition-transform', enabled ? 'translate-x-6' : 'translate-x-1')} />
        </button>
      </div>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Similarity Threshold</label>
          <span className="text-sm text-muted-foreground">{threshold.toFixed(2)}</span>
        </div>
        <input type="range" min={0.5} max={1.0} step={0.01} value={threshold} onChange={(e) => setThreshold(Number(e.target.value))} className="w-full accent-primary" />
        <p className="mt-1 text-xs text-muted-foreground">Higher values require closer matches before serving a cached result.</p>
      </div>
      <div>
        <div className="mb-1 flex justify-between">
          <label className="text-sm font-medium text-foreground">Cache Max Age</label>
          <span className="text-sm text-muted-foreground">{maxAgeDays} days</span>
        </div>
        <input type="range" min={1} max={365} step={1} value={maxAgeDays} onChange={(e) => setMaxAgeDays(Number(e.target.value))} className="w-full accent-primary" />
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
              onClick={() => {
                const next = !schemaPruningEnabled;
                setSchemaPruningEnabled(next);
                update.mutate(
                  { schema_pruning_enabled: next },
                  { onError: () => setSchemaPruningEnabled(!next) },
                );
              }}
              className={cn(
                'relative inline-flex h-6 w-11 flex-shrink-0 items-center rounded-full transition-colors',
                schemaPruningEnabled ? 'bg-primary' : 'bg-muted-foreground',
              )}
            >
              <span className={cn('inline-block h-4 w-4 transform rounded-full bg-primary-foreground transition-transform', schemaPruningEnabled ? 'translate-x-6' : 'translate-x-1')} />
            </button>
          </div>
          <div>
            <div className="mb-1 flex justify-between">
              <label className="text-sm font-medium text-foreground">Max Tables (Top K)</label>
              <span className="text-sm text-muted-foreground">{schemaPruningTopK}</span>
            </div>
            <input type="range" min={3} max={40} step={1} value={schemaPruningTopK} onChange={(e) => setSchemaPruningTopK(Number(e.target.value))} className="w-full accent-primary" />
            <p className="mt-1 text-xs text-muted-foreground">Maximum number of tables passed to the LLM. Lower values reduce token usage; increase if queries span many tables.</p>
          </div>
        </div>
      </div>
      <div className="flex items-center gap-3">
        <button onClick={handleSave} disabled={update.isPending} className="rounded-md bg-brand-gradient px-4 py-1.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50">
          {update.isPending ? 'Saving…' : 'Save'}
        </button>
        {update.isSuccess && <span className="text-xs text-success">✓ Saved</span>}
        {update.isError && (
          <span className="text-xs text-destructive">
            {(update.error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? 'Failed to save'}
          </span>
        )}
      </div>
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
        <textarea
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="SQL query"
          rows={3}
          className="w-full rounded-md border border-border bg-background px-3 py-1.5 font-mono text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        />
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
  const [activeTab, setActiveTab] = useState<Tab>('providers');
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

  const tabs: { id: Tab; label: string }[] = [
    { id: 'providers', label: 'LLM Providers' },
    { id: 'safety', label: 'Query Safety' },
    { id: 'cache', label: 'Cache' },
    { id: 'examples', label: 'Examples Library' },
  ];

  return (
    <div className="flex-1 overflow-auto bg-background">
      <div className="mx-auto max-w-3xl px-4 py-8">
        <h1 className="mb-6 font-display text-2xl font-bold text-foreground">Settings</h1>

        {/* Tabs */}
        <div role="tablist" aria-label="Settings sections" className="mb-6 flex gap-1 border-b border-border">
          {tabs.map((tab) => (
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

          {activeTab === 'safety' && <QuerySafetyTab />}

          {activeTab === 'cache' && (
            <div>
              <CacheSettingsSection />
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
