// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, CheckCircle, Database, RefreshCw, XCircle, Zap } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import { Label } from '../components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import DynamicConnectionForm from '../components/DynamicConnectionForm'
import { datasourcesApi } from '../api/datasources'
import { getDatasourceIcon } from '@/lib/datasourceIcons'
import { useProviders, useCreateProvider, useFetchModels } from '../hooks/useProviders'
import type { DataSourceInfo } from '../types'

const WIZARD_DONE_KEY = 'savvina-wizard-done'

type Step = 1 | 2

interface StepMeta {
  step: Step
  icon: React.ReactNode
  title: string
  description: string
}

const STEPS: StepMeta[] = [
  {
    step: 1,
    icon: <Database className="h-5 w-5" />,
    title: 'Connect a database',
    description: 'Add your first data source',
  },
  {
    step: 2,
    icon: <Zap className="h-5 w-5" />,
    title: 'Configure an LLM',
    description: 'Choose your AI provider',
  },
]

export default function SetupWizardPage() {
  const navigate = useNavigate()
  const [step, setStep] = useState<Step>(1)

  useEffect(() => {
    if (localStorage.getItem(WIZARD_DONE_KEY)) {
      navigate('/connect', { replace: true })
    }
  }, [navigate])

  const advance = () => {
    if (step < 2) {
      setStep((s) => (s + 1) as Step)
    } else {
      finish()
    }
  }

  const finish = () => {
    localStorage.setItem(WIZARD_DONE_KEY, '1')
    navigate('/connect', { replace: true })
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-surface-sunken p-6">
      <div className="mb-8 text-center">
        <h1 className="font-display text-2xl font-semibold text-foreground">
          Let&apos;s get <span className="savvina-grad-text">savvina ai</span> set up
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">Step {step} of 2</p>
      </div>

      {/* Step indicator */}
      <div className="mb-8 flex items-center">
        {STEPS.map(({ step: s }, idx) => (
          <div key={s} className="flex items-center">
            <div
              className={cn(
                'flex h-8 w-8 items-center justify-center rounded-full border-2 text-sm font-medium transition-colors',
                s < step && 'border-primary bg-primary text-primary-foreground',
                s === step && 'border-primary text-primary',
                s > step && 'border-border text-muted-foreground',
              )}
            >
              {s < step ? <Check className="h-4 w-4" /> : s}
            </div>
            {idx < STEPS.length - 1 && (
              <div className={cn('mx-2 h-px w-12', s < step ? 'bg-primary' : 'bg-border')} />
            )}
          </div>
        ))}
      </div>

      <Card className="w-full max-w-lg">
        {step === 1 && <Step1Database onSave={advance} onSkip={advance} />}
        {step === 2 && <Step2Provider onSave={finish} onSkip={finish} />}
      </Card>
    </div>
  )
}

// ── Step 1: Database connection ───────────────────────────────────────────────

interface StepProps {
  onSave: () => void
  onSkip: () => void
}

interface TestResult {
  success: boolean
  message: string
  server_version?: string
}

function Step1Database({ onSave, onSkip }: StepProps) {
  const [name, setName] = useState('')
  const [selectedSource, setSelectedSource] = useState<DataSourceInfo | null>(null)
  const [config, setConfig] = useState<Record<string, unknown>>({})
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestResult | null>(null)

  const { data: sources, isLoading: sourcesLoading } = useQuery({
    queryKey: ['datasources'],
    queryFn: datasourcesApi.getAvailable,
  })

  useEffect(() => {
    if (sources && sources.length > 0 && !selectedSource) {
      setSelectedSource(sources[0])
    }
  }, [sources, selectedSource])

  const handleSourceChange = (sourceType: string) => {
    const src = sources?.find((s) => s.source_type === sourceType) ?? null
    setSelectedSource(src)
    setConfig({})
    setTestResult(null)
  }

  const handleTest = async () => {
    if (!selectedSource) return
    setError(null)
    setTestResult(null)
    setTesting(true)
    try {
      const { connectionsApi } = await import('../api/connections')
      const result = await connectionsApi.testNew(selectedSource.source_type, config)
      setTestResult(result)
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? ''
      setTestResult({ success: false, message: detail || 'Connection test failed.' })
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedSource) return
    setError(null)
    setTestResult(null)
    setSaving(true)
    try {
      const { connectionsApi } = await import('../api/connections')
      await connectionsApi.create({
        name: name || `${selectedSource.display_name} Connection`,
        source_type: selectedSource.source_type,
        config,
      })
      onSave()
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? ''
      setError(detail || 'Could not save connection. You can skip and add it later.')
    } finally {
      setSaving(false)
    }
  }

  const busy = saving || testing

  return (
    <>
      <CardHeader>
        <CardTitle>Connect a database</CardTitle>
        <CardDescription>Add your first data source. You can add more in Settings.</CardDescription>
      </CardHeader>
      <CardContent>
        {sourcesLoading ? (
          <div className="flex items-center justify-center py-8">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        ) : (
          <form onSubmit={handleSave} autoComplete="off" className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="connName">Connection name</Label>
              <Input
                id="connName"
                placeholder={selectedSource ? `My ${selectedSource.display_name}` : 'My Database'}
                value={name}
                autoComplete="off"
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>Database type</Label>
              <Select value={selectedSource?.source_type ?? ''} onValueChange={handleSourceChange}>
                <SelectTrigger>
                  {selectedSource ? (
                    <div className="flex items-center gap-2">
                      {getDatasourceIcon(selectedSource.source_type) ? (
                        <img
                          src={getDatasourceIcon(selectedSource.source_type)!}
                          alt=""
                          className="h-4 w-4 shrink-0 object-contain"
                        />
                      ) : (
                        <span>{selectedSource.icon}</span>
                      )}
                      <span>{selectedSource.display_name}</span>
                    </div>
                  ) : (
                    <SelectValue placeholder="Select a database type" />
                  )}
                </SelectTrigger>
                <SelectContent>
                  {sources?.map((src) => (
                    <SelectItem key={src.source_type} value={src.source_type}>
                      <div className="flex items-center gap-2">
                        {getDatasourceIcon(src.source_type) ? (
                          <img
                            src={getDatasourceIcon(src.source_type)!}
                            alt=""
                            className="h-4 w-4 shrink-0 object-contain"
                          />
                        ) : (
                          <span>{src.icon}</span>
                        )}
                        {src.display_name}
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {selectedSource && (
              <DynamicConnectionForm
                schema={selectedSource.config_schema}
                values={config}
                onChange={(vals) => { setConfig(vals); setTestResult(null) }}
              />
            )}
            {testResult && (
              <div
                className={cn(
                  'flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm',
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
            {error && <p className="text-sm text-destructive">{error}</p>}
            <div className="flex gap-2">
              <Button
                type="button"
                variant="default"
                disabled={busy || !selectedSource}
                onClick={handleTest}
              >
                {testing ? 'Testing…' : 'Test'}
              </Button>
              <Button type="submit" variant="gradient" disabled={busy || !selectedSource} className="flex-1">
                {saving ? 'Saving…' : 'Save & Continue'}
              </Button>
              <Button type="button" variant="ghost" onClick={onSkip} title="You can skip for now, but chat won't work until a database is configured">
                Skip
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </>
  )
}

// ── Step 2: LLM provider ──────────────────────────────────────────────────────

function Step2Provider({ onSave, onSkip }: StepProps) {
  const { data: providers, isLoading: providersLoading } = useProviders()
  const create = useCreateProvider()

  const availableProviders = (providers ?? []).filter(
    (p) => p.provider_type !== 'openai_compatible',
  )

  const [providerType, setProviderType] = useState<string>('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<TestResult | null>(null)
  const [dynamicModels, setDynamicModels] = useState<string[]>([])
  const [fetchModelsError, setFetchModelsError] = useState<string | null>(null)
  const fetchModelsMutation = useFetchModels()

  const selected = availableProviders.find((p) => p.provider_type === providerType) ?? null
  const isOllama = providerType === 'ollama'

  // Seed the form from the first available provider once the list loads
  useEffect(() => {
    if (!providerType && availableProviders.length > 0) {
      const first = availableProviders[0]
      setProviderType(first.provider_type)
      setModel(first.available_models[0] ?? first.current_model ?? '')
      setDisplayName(first.provider_display_name)
      setBaseUrl(first.base_url ?? '')
    }
  }, [availableProviders, providerType])

  const handleProviderChange = (val: string) => {
    const next = availableProviders.find((p) => p.provider_type === val)
    setProviderType(val)
    setModel(next?.available_models[0] ?? next?.current_model ?? '')
    setDisplayName(next?.provider_display_name ?? '')
    setBaseUrl(next?.base_url ?? '')
    setTestResult(null)
    setDynamicModels([])
    setFetchModelsError(null)
  }

  const handleFetchModels = async () => {
    setFetchModelsError(null)
    try {
      const models = await fetchModelsMutation.mutateAsync({
        provider_type: providerType,
        api_key: isOllama ? undefined : apiKey || undefined,
        base_url: isOllama ? baseUrl || undefined : undefined,
      })
      if (models.length === 0) {
        setFetchModelsError('No models returned — check your API key')
      } else {
        setDynamicModels(models)
        setModel(models[0])
      }
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setFetchModelsError(detail ?? 'Failed to fetch models')
    }
  }

  const canFetchModels = isOllama ? true : apiKey.length > 5

  const handleTest = async () => {
    if (!selected) return
    setError(null)
    setTestResult(null)
    setTesting(true)
    try {
      const { providersApi } = await import('../api/providers')
      const data = await providersApi.testNew({
        provider_type: selected.provider_type,
        api_key: isOllama ? undefined : apiKey || undefined,
        model: model || undefined,
        base_url: isOllama ? baseUrl || undefined : undefined,
      })
      setTestResult(data)
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? ''
      setTestResult({ success: false, message: detail || 'Connection test failed.' })
    } finally {
      setTesting(false)
    }
  }

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selected) return
    setError(null)
    setTestResult(null)
    try {
      await create.mutateAsync({
        provider_type: selected.provider_type,
        display_name: displayName || selected.provider_display_name,
        api_key: isOllama ? undefined : apiKey || undefined,
        base_url: isOllama ? baseUrl || undefined : undefined,
        model: model || undefined,
        is_active: true,
      })
      onSave()
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? ''
      setError(detail || 'Could not save provider. You can skip and configure it later.')
    }
  }

  const busy = create.isPending || testing
  const canSubmit = !!selected && (isOllama || apiKey.length > 0)

  return (
    <>
      <CardHeader>
        <CardTitle>Configure an LLM</CardTitle>
        <CardDescription>
          Choose an AI provider to power natural-language queries.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {providersLoading ? (
          <div className="flex items-center justify-center py-8">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        ) : (
          <form onSubmit={handleSave} className="space-y-4">
            <div className="space-y-2">
              <Label>Provider</Label>
              <Select value={providerType} onValueChange={handleProviderChange}>
                <SelectTrigger>
                  <SelectValue placeholder="Select a provider" />
                </SelectTrigger>
                <SelectContent>
                  {availableProviders.map((p) => (
                    <SelectItem key={p.provider_type} value={p.provider_type}>
                      {p.provider_display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {!isOllama && (
              <div className="space-y-2">
                <Label htmlFor="apiKey">API key</Label>
                <div className="flex gap-2">
                  <Input
                    id="apiKey"
                    type="password"
                    placeholder="sk-…"
                    value={apiKey}
                    onChange={(e) => {
                      setApiKey(e.target.value)
                      setTestResult(null)
                    }}
                    autoComplete="off"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    disabled={!canFetchModels || fetchModelsMutation.isPending}
                    onClick={handleFetchModels}
                  >
                    {fetchModelsMutation.isPending ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <><RefreshCw className="mr-1.5 h-3.5 w-3.5" />Fetch Models</>
                    )}
                  </Button>
                </div>
              </div>
            )}
            {isOllama && (
              <div className="space-y-2">
                <Label htmlFor="baseUrl">Base URL</Label>
                <div className="flex gap-2">
                  <Input
                    id="baseUrl"
                    placeholder="http://localhost:11434"
                    value={baseUrl}
                    onChange={(e) => {
                      setBaseUrl(e.target.value)
                      setTestResult(null)
                    }}
                    autoComplete="off"
                  />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    disabled={fetchModelsMutation.isPending}
                    onClick={handleFetchModels}
                  >
                    {fetchModelsMutation.isPending ? (
                      <RefreshCw className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <><RefreshCw className="mr-1.5 h-3.5 w-3.5" />Fetch Models</>
                    )}
                  </Button>
                </div>
              </div>
            )}
            <div className="space-y-2">
              <Label htmlFor="llmModel">Model</Label>
              {(() => {
                const modelList = dynamicModels.length > 0 ? dynamicModels : (selected?.available_models ?? [])
                return modelList.length > 0 ? (
                  <Select
                    value={model}
                    onValueChange={(v) => {
                      setModel(v)
                      setTestResult(null)
                    }}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder="Select a model" />
                    </SelectTrigger>
                    <SelectContent>
                      {modelList.map((m) => (
                        <SelectItem key={m} value={m}>
                          {m}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <Input
                    id="llmModel"
                    placeholder={selected?.current_model ?? ''}
                    value={model}
                    onChange={(e) => {
                      setModel(e.target.value)
                      setTestResult(null)
                    }}
                  />
                )
              })()}
              {fetchModelsError && (
                <p className="text-xs text-destructive">{fetchModelsError}</p>
              )}
            </div>
            {testResult && (
              <div
                className={cn(
                  'flex items-start gap-2 rounded-lg border px-3 py-2.5 text-sm',
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
                <span className="break-all">{testResult.message}</span>
              </div>
            )}
            {error && <p className="text-sm text-destructive">{error}</p>}
            <div className="flex gap-2">
              <Button
                type="button"
                variant="default"
                disabled={busy || !canSubmit}
                onClick={handleTest}
              >
                {testing ? 'Testing…' : 'Test'}
              </Button>
              <Button type="submit" variant="gradient" disabled={busy || !canSubmit} className="flex-1">
                {create.isPending ? 'Saving…' : 'Save & Continue'}
              </Button>
              <Button type="button" variant="ghost" onClick={onSkip} title="You can skip for now, but chat won't work until a provider is configured">
                Skip
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </>
  )
}

