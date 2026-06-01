// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '../components/ui/button';
import { MoreHorizontal, ArrowLeft, CheckCircle, XCircle } from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import DataSourceSelector from '../components/DataSourceSelector';
import DynamicConnectionForm from '../components/DynamicConnectionForm';
import PrivacySettingsForm from '../components/PrivacySettings';
import ExecutionModeSelector from '../components/ExecutionModeSelector';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../components/ui/dropdown-menu';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../components/ui/dialog';
import { connectionsApi } from '../api/connections';
import apiClient from '../api/client';
import {
  useConnections,
  useConnection,
  useCreateConnection,
  useDeleteConnection,
  useTestNewConnection,
  useUpdatePrivacySettings,
  useUpdateExecutionMode,
} from '../hooks/useConnections';
import { useAppStore } from '../store/appStore';
import { cn } from '@/lib/utils';
import { getDatasourceIcon } from '@/lib/datasourceIcons';
import type { DataSourceInfo, PrivacySettings, Connection } from '../types';

const DEFAULT_PRIVACY: PrivacySettings = {
  include_sample_values: false,
  include_column_comments: false,
  include_row_counts: false,
  sensitive_column_patterns: [
    'password', 'passwd', 'secret', 'token', 'api_key',
    'email', 'ssn', 'social_security', 'credit_card', 'card_number', 'cvv',
    'phone', 'mobile', 'address', 'salary', 'wage', 'income',
    'bank_account', 'routing_number', 'dob', 'date_of_birth',
    'national_id', 'passport', 'license_number', 'tax_id',
  ],
  excluded_schemas: [],
  excluded_tables: [],
  excluded_columns: [],
};

function EditPanel({ connId, onClose }: { connId: string; onClose: () => void }) {
  const { data: detail, isLoading } = useConnection(connId);
  const updatePrivacy = useUpdatePrivacySettings();
  const updateMode = useUpdateExecutionMode();
  const [privacy, setPrivacy] = useState<PrivacySettings>(DEFAULT_PRIVACY);
  const [execMode, setExecMode] = useState<Connection['execution_mode']>('auto_execute');
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [initialized, setInitialized] = useState(false);

  useEffect(() => {
    if (detail && !initialized) {
      if (detail.privacy_settings) setPrivacy(detail.privacy_settings);
      setExecMode(detail.execution_mode);
      setInitialized(true);
    }
  }, [detail, initialized]);

  const handleSave = async () => {
    await Promise.all([
      updatePrivacy.mutateAsync({ id: connId, settings: privacy }),
      updateMode.mutateAsync({ id: connId, mode: execMode }),
    ]);
    onClose();
  };

  if (isLoading) {
    return (
      <div className="border-t border-border px-4 py-3 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  const isSaving = updatePrivacy.isPending || updateMode.isPending;

  return (
    <div className="space-y-4 border-t border-border bg-muted/30 px-4 py-4">
      <div>
        <button
          onClick={() => setPrivacyOpen((o) => !o)}
          className="flex items-center gap-2 text-sm font-medium text-foreground hover:text-muted-foreground"
        >
          <span>{privacyOpen ? '▼' : '▶'}</span>
          Privacy Settings
        </button>
        {privacyOpen && (
          <div className="mt-4 border-l-2 border-border pl-4">
            <PrivacySettingsForm settings={privacy} onChange={setPrivacy} />
          </div>
        )}
      </div>
      <div>
        <p className="mb-3 text-sm font-medium text-foreground">Execution Mode</p>
        <ExecutionModeSelector value={execMode} onChange={setExecMode} />
      </div>
      <div className="flex gap-2">
        <Button
          variant="gradient"
          onClick={handleSave}
          disabled={isSaving}
        >
          {isSaving ? 'Saving…' : 'Save'}
        </Button>
        <button
          onClick={onClose}
          className="rounded-md border border-border px-4 py-1.5 text-sm text-foreground transition-colors hover:bg-muted"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function EditConnectionPanel({ conn, onClose }: { conn: Connection; onClose: () => void }) {
  const queryClient = useQueryClient();
  const user = useAuthStore((s) => s.user);
  const isAdmin = !!user;

  const testConn = useTestNewConnection();

  const [name, setName] = useState(conn.name);
  const [configValues, setConfigValues] = useState<Record<string, unknown>>({});
  const [schema, setSchema] = useState<DataSourceInfo | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<{
    success: boolean;
    message: string;
    server_version?: string;
  } | null>(null);

  useEffect(() => {
    // Load datasource schema for all users (needed to render the excludeFields list).
    // Load connection config only for admins.
    const dsPromise = apiClient
      .get<DataSourceInfo[]>('/api/v1/datasources')
      .then((dsRes) => {
        const src = dsRes.data.find((s) => s.source_type === conn.source_type) ?? null;
        setSchema(src);
      });

    if (!isAdmin) {
      dsPromise.catch(() => {}).finally(() => setIsLoading(false));
      return;
    }

    Promise.all([
      dsPromise,
      apiClient.get<{ name: string; source_type: string; config: Record<string, unknown> }>(
        `/api/v1/connections/${conn.id}/config`,
      ),
    ])
      .then(([, cfgRes]) => {
        setConfigValues(cfgRes.data.config);
        setName(cfgRes.data.name);
      })
      .catch(() => setFetchError('Failed to load connection details.'))
      .finally(() => setIsLoading(false));
  }, [conn.id, conn.source_type, isAdmin]);

  const handleTest = async () => {
    if (!schema) return;
    setTestResult(null);
    setError(null);
    try {
      const result = await testConn.mutateAsync({
        sourceType: conn.source_type,
        config: configValues,
      });
      setTestResult(result);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? 'Test failed');
    }
  };

  const handleSave = async () => {
    setIsSaving(true);
    setError(null);
    try {
      if (isAdmin) {
        await apiClient.put(`/api/v1/connections/${conn.id}/config`, {
          name,
          config: configValues,
        });
        await queryClient.invalidateQueries({ queryKey: ['connections'] });
      }
      onClose();
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? 'Failed to save');
    } finally {
      setIsSaving(false);
    }
  };

  if (isLoading) {
    return (
      <div className="border-t border-border px-4 py-3 text-sm text-muted-foreground">
        Loading…
      </div>
    );
  }

  if (fetchError) {
    return (
      <div className="border-t border-border px-4 py-3">
        <div className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <p className="text-sm text-destructive">{fetchError}</p>
        </div>
      </div>
    );
  }

  const isBusy = isSaving || testConn.isPending;

  return (
    <div className="border-t border-border bg-muted/30 px-4 py-4">
      {/* ── Server Settings (admin only) ─────────────────────────────── */}
      {isAdmin && (
        <div className="space-y-4">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Server Settings
          </h3>
          <div>
            <label htmlFor="conn-name-edit" className="mb-1.5 block text-sm font-medium text-foreground">
              Connection name
            </label>
            <input
              id="conn-name-edit"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
            />
          </div>
          {schema && (
            <DynamicConnectionForm
              schema={schema.config_schema}
              values={configValues}
              onChange={setConfigValues}
              excludeFields={[]}
            />
          )}
        </div>
      )}

      {/* ── Feedback ─────────────────────────────────────────────────── */}
      {error && (
        <div className="mt-4 flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      )}

      {testResult && (
        <div
          className={cn(
            'mt-4 flex items-start gap-2 rounded-xl border px-4 py-3 text-sm',
            testResult.success
              ? 'border-success/30 bg-success/10 text-success'
              : 'border-destructive/30 bg-destructive/10 text-destructive',
          )}
        >
          {testResult.success ? (
            <CheckCircle className="mt-0.5 h-4 w-4 shrink-0" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
          )}
          <span>
            {testResult.message}
            {testResult.server_version && (
              <span className="ml-2 opacity-70">({testResult.server_version})</span>
            )}
          </span>
        </div>
      )}

      {/* ── Actions ──────────────────────────────────────────────────── */}
      <div className="mt-4 flex gap-2">
        {isAdmin && (
          <Button
            variant="default"
            onClick={handleTest}
            disabled={isBusy || testConn.isPending}
          >
            {testConn.isPending ? 'Testing…' : 'Test'}
          </Button>
        )}
        <Button
          variant="gradient"
          onClick={handleSave}
          disabled={isBusy}
        >
          {isSaving ? 'Saving…' : 'Save'}
        </Button>
        <button
          onClick={onClose}
          className="rounded-md border border-border px-4 py-1.5 text-sm text-foreground transition-colors hover:bg-muted"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

function ConnectionCard({
  conn,
  onUse,
  onEdit,
  onEditConn,
  onSemantic,
  onDelete,
  isEditing,
  onCloseEdit,
  isEditingConn,
  onCloseEditConn,
}: {
  conn: Connection;
  onUse: () => void;
  onEdit: () => void;
  onEditConn: () => void;
  onSemantic: () => void;
  onDelete: () => void;
  isEditing: boolean;
  onCloseEdit: () => void;
  isEditingConn: boolean;
  onCloseEditConn: () => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card transition-shadow hover:shadow-sm">
      <div className="flex items-center gap-3 px-4 py-4">
        {/* Status dot */}
        <div className="h-2 w-2 shrink-0 rounded-full bg-success" />

        {/* Datasource icon */}
        {getDatasourceIcon(conn.source_type) && (
          <img
            src={getDatasourceIcon(conn.source_type)!}
            alt={conn.source_type}
            className="h-7 w-7 shrink-0 object-contain"
          />
        )}

        {/* Info */}
        <div className="flex-1 min-w-0">
          <p className="truncate text-sm font-medium text-foreground">{conn.name}</p>
          <div className="mt-1 flex items-center gap-2">
            <span className="rounded bg-badge-bg px-1.5 py-0.5 font-mono text-[10px] text-badge-text uppercase">
              {conn.source_type}
            </span>
            <span className="font-mono text-[10px] text-muted-foreground capitalize">
              {conn.execution_mode.replace(/_/g, ' ')}
            </span>
          </div>
        </div>

        {/* Use button */}
        <button
          onClick={onUse}
          className="shrink-0 rounded-md bg-brand-gradient px-3 py-1.5 text-xs font-medium text-white shadow-gradient-btn transition-opacity hover:opacity-90"
        >
          Use
        </button>

        {/* ··· dropdown */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="shrink-0 rounded-md p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
              <MoreHorizontal className="h-4 w-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onEdit}>Edit settings</DropdownMenuItem>
            <DropdownMenuItem onClick={onEditConn}>Edit connection</DropdownMenuItem>
            <DropdownMenuItem onClick={onSemantic}>Semantic model</DropdownMenuItem>
            <DropdownMenuItem
              onClick={onDelete}
              className="text-destructive focus:text-destructive"
            >
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {isEditing && <EditPanel connId={conn.id} onClose={onCloseEdit} />}
      {isEditingConn && <EditConnectionPanel conn={conn} onClose={onCloseEditConn} />}
    </div>
  );
}

export default function ConnectionPage() {
  const navigate = useNavigate();
  const { data: connections } = useConnections();
  const createConn = useCreateConnection();
  const deleteConn = useDeleteConnection();
  const testConn = useTestNewConnection();
  const { setActiveConnection } = useAppStore();

  const [step, setStep] = useState<'select' | 'form'>('select');
  const [selectedSource, setSelectedSource] = useState<DataSourceInfo | null>(null);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingConnId, setEditingConnId] = useState<string | null>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const [name, setName] = useState('');
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [privacy, setPrivacy] = useState<PrivacySettings>(DEFAULT_PRIVACY);
  const [execMode, setExecMode] = useState<Connection['execution_mode']>('auto_execute');
  const [testResult, setTestResult] = useState<{
    success: boolean;
    message: string;
    server_version?: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isSavingFlow, setIsSavingFlow] = useState(false);

  const isMounted = useRef(true);
  useEffect(() => {
    isMounted.current = true;
    return () => { isMounted.current = false; };
  }, []);

  const handleSelectSource = (source: DataSourceInfo) => {
    setSelectedSource(source);
    setConfig({});
    setName('');
    setStep('form');
  };

  const handleTest = async () => {
    if (!selectedSource) return;
    setTestResult(null);
    setError(null);
    try {
      const result = await testConn.mutateAsync({
        sourceType: selectedSource.source_type,
        config,
      });
      setTestResult(result);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? 'Test failed');
    }
  };

  const handleSave = async () => {
    if (!selectedSource) return;
    setTestResult(null);
    setError(null);
    setIsSavingFlow(true);
    try {
      const testRes = await testConn.mutateAsync({
        sourceType: selectedSource.source_type,
        config,
      });
      if (!isMounted.current) return;
      setTestResult(testRes);
    } catch (e: unknown) {
      if (!isMounted.current) return;
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? 'Connection test failed — check your credentials');
      setIsSavingFlow(false);
      return;
    }
    try {
      const newConn = await createConn.mutateAsync({
        name: name || `${selectedSource.display_name} Connection`,
        source_type: selectedSource.source_type,
        config,
        privacy_settings: privacy,
        execution_mode: execMode,
      });
      try {
        await connectionsApi.refreshSchema(newConn.id);
      } catch {
        // non-fatal
      }
      navigate('/chat');
    } catch (e: unknown) {
      if (!isMounted.current) return;
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(detail ?? 'Failed to save connection');
    } finally {
      if (isMounted.current) setIsSavingFlow(false);
    }
  };

  return (
    <div className="flex-1 overflow-auto bg-background">
      <div className="mx-auto max-w-3xl px-4 py-8">
        {step === 'select' ? (
          <div className="space-y-8">
            <div>
              <h1 className="font-display text-2xl font-bold text-foreground">
                Connect a Data Source
              </h1>
              <p className="mt-1 text-sm text-muted-foreground">
                Choose a source type to get started.
              </p>
            </div>

            <DataSourceSelector onSelect={handleSelectSource} />

            {connections && connections.length > 0 && (
              <div>
                <h2 className="mb-4 font-display text-base font-semibold text-foreground">
                  Existing Connections
                </h2>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                  {connections.map((conn) => (
                    <ConnectionCard
                      key={conn.id}
                      conn={conn}
                      onUse={() => {
                        setActiveConnection(conn.id);
                        navigate('/chat');
                      }}
                      onEdit={() => setEditingId(editingId === conn.id ? null : conn.id)}
                      onEditConn={() =>
                        setEditingConnId(editingConnId === conn.id ? null : conn.id)
                      }
                      onSemantic={() => navigate(`/semantic/${conn.id}`)}
                      onDelete={() => setDeleteConfirmId(conn.id)}
                      isEditing={editingId === conn.id}
                      onCloseEdit={() => setEditingId(null)}
                      isEditingConn={editingConnId === conn.id}
                      onCloseEditConn={() => setEditingConnId(null)}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="space-y-6">
            <div className="flex items-center gap-3">
              <button
                onClick={() => setStep('select')}
                className="flex items-center gap-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                <ArrowLeft className="h-4 w-4" />
                Back
              </button>
              <h1 className="flex items-center gap-2 font-display text-2xl font-bold text-foreground">
                {selectedSource && getDatasourceIcon(selectedSource.source_type) ? (
                  <img
                    src={getDatasourceIcon(selectedSource.source_type)!}
                    alt={selectedSource.display_name}
                    className="h-7 w-7 object-contain"
                  />
                ) : (
                  <span>{selectedSource?.icon}</span>
                )}
                {selectedSource?.display_name}
              </h1>
            </div>

            <div>
              <label htmlFor="conn-name-new" className="mb-1.5 block text-sm font-medium text-foreground">
                Connection name
              </label>
              <input
                id="conn-name-new"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={`My ${selectedSource?.display_name}`}
                autoComplete="off"
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>

            {selectedSource && (
              <DynamicConnectionForm
                schema={selectedSource.config_schema}
                values={config}
                onChange={setConfig}
              />
            )}

            <div>
              <button
                onClick={() => setPrivacyOpen((o) => !o)}
                className="flex items-center gap-2 text-sm font-medium text-foreground hover:text-muted-foreground"
              >
                <span>{privacyOpen ? '▼' : '▶'}</span>
                Privacy Settings
              </button>
              {privacyOpen && (
                <div className="mt-4 border-l-2 border-border pl-4">
                  <PrivacySettingsForm settings={privacy} onChange={setPrivacy} />
                </div>
              )}
            </div>

            <div>
              <p className="mb-3 text-sm font-medium text-foreground">Execution Mode</p>
              <ExecutionModeSelector value={execMode} onChange={setExecMode} />
            </div>

            {error && (
              <div className="flex items-start gap-2 rounded-xl border border-destructive/30 bg-destructive/10 px-4 py-3">
                <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                <p className="text-sm text-destructive">{error}</p>
              </div>
            )}

            {testResult && (
              <div
                className={cn(
                  'flex items-start gap-2 rounded-xl border px-4 py-3 text-sm',
                  testResult.success
                    ? 'border-success/30 bg-success/10 text-success'
                    : 'border-destructive/30 bg-destructive/10 text-destructive',
                )}
              >
                {testResult.success ? (
                  <CheckCircle className="mt-0.5 h-4 w-4 shrink-0" />
                ) : (
                  <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
                )}
                <span>
                  {testResult.message}
                  {testResult.server_version && (
                    <span className="ml-2 opacity-70">({testResult.server_version})</span>
                  )}
                </span>
              </div>
            )}

            <div className="flex gap-3">
              <Button
                variant="default"
                onClick={handleTest}
                disabled={testConn.isPending || isSavingFlow}
              >
                {testConn.isPending && !isSavingFlow ? 'Testing…' : 'Test Connection'}
              </Button>
              {(() => {
                const hasRequiredFields = selectedSource
                  ? selectedSource.config_schema.fields
                      .filter((f) => f.required)
                      .every((f) => {
                        const v = config[f.name];
                        return v !== undefined && v !== null && String(v).trim() !== '';
                      })
                  : false;
                const hasName = name.trim() !== '';
                return (
                  <Button
                    variant="gradient"
                    size="lg"
                    onClick={handleSave}
                    disabled={testConn.isPending || createConn.isPending || !hasRequiredFields || !hasName}
                  >
                    {isSavingFlow && testConn.isPending
                      ? 'Verifying…'
                      : createConn.isPending
                        ? 'Saving…'
                        : 'Save & Connect'}
                  </Button>
                );
              })()}
            </div>
          </div>
        )}
      </div>

      <Dialog open={deleteConfirmId !== null} onOpenChange={(open) => { if (!open) setDeleteConfirmId(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete connection?</DialogTitle>
            <DialogDescription>
              This will permanently delete{' '}
              <strong>{connections?.find((c) => c.id === deleteConfirmId)?.name ?? 'this connection'}</strong>{' '}
              and all its chat history, cache entries, and verified examples. This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <button
              onClick={() => setDeleteConfirmId(null)}
              className="rounded-md border border-border px-4 py-2 text-sm hover:bg-muted"
            >
              Cancel
            </button>
            <button
              onClick={() => {
                if (deleteConfirmId) {
                  deleteConn.mutate(deleteConfirmId);
                  setDeleteConfirmId(null);
                }
              }}
              disabled={deleteConn.isPending}
              className="rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground hover:bg-destructive/90 disabled:opacity-50"
            >
              {deleteConn.isPending ? 'Deleting…' : 'Delete'}
            </button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
