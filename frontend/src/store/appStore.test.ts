// Copyright (c) 2025 Savvina AI Ltd
// Licensed under the Business Source License 1.1 — see LICENSE for details.

import { describe, it, expect, beforeEach } from 'vitest';
import { useAppStore } from './appStore';
import { makeChatMessage } from '../test/factories';

const resetStore = () =>
  useAppStore.setState({
    activeConnectionId: null,
    activeSessionId: null,
    selectedProvider: '',
    schema: null,
    messages: [],
  });

describe('appStore', () => {
  beforeEach(() => {
    resetStore();
    localStorage.clear();
  });

  it('setActiveConnection sets activeConnectionId', () => {
    useAppStore.getState().setActiveConnection('conn-abc');
    expect(useAppStore.getState().activeConnectionId).toBe('conn-abc');
  });

  it('setActiveConnection resets activeSessionId to null', () => {
    useAppStore.setState({ activeSessionId: 'session-xyz' });
    useAppStore.getState().setActiveConnection('conn-abc');
    expect(useAppStore.getState().activeSessionId).toBeNull();
  });

  it('setActiveConnection resets messages to []', () => {
    useAppStore.setState({ messages: [makeChatMessage()] });
    useAppStore.getState().setActiveConnection('conn-abc');
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it('addMessage appends message to messages array', () => {
    const msg = makeChatMessage({ id: 'msg-1' });
    useAppStore.getState().addMessage(msg);
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0]).toEqual(msg);
  });

  it('addMessage does not mutate existing messages', () => {
    const first = makeChatMessage({ id: 'msg-1' });
    const second = makeChatMessage({ id: 'msg-2' });
    useAppStore.getState().addMessage(first);
    const snapshotBefore = useAppStore.getState().messages;
    useAppStore.getState().addMessage(second);
    const snapshotAfter = useAppStore.getState().messages;
    // The array reference should be different (immutable update)
    expect(snapshotBefore).not.toBe(snapshotAfter);
    expect(snapshotBefore).toHaveLength(1);
    expect(snapshotAfter).toHaveLength(2);
  });

  it('updateMessage patches the matching message by ID', () => {
    const msg = makeChatMessage({ id: 'msg-1', status: 'pending_approval' });
    useAppStore.setState({ messages: [msg] });
    useAppStore.getState().updateMessage('msg-1', { status: 'executed' });
    expect(useAppStore.getState().messages[0].status).toBe('executed');
  });

  it('updateMessage does not touch other messages', () => {
    const msg1 = makeChatMessage({ id: 'msg-1', status: 'pending_approval' });
    const msg2 = makeChatMessage({ id: 'msg-2', status: 'executed' });
    useAppStore.setState({ messages: [msg1, msg2] });
    useAppStore.getState().updateMessage('msg-1', { status: 'executed' });
    expect(useAppStore.getState().messages[1].status).toBe('executed');
    expect(useAppStore.getState().messages[1].id).toBe('msg-2');
  });

  it('promoteSession sets activeSessionId without clearing messages', () => {
    const msg = makeChatMessage({ id: 'msg-1' });
    useAppStore.setState({ messages: [msg] });
    useAppStore.getState().promoteSession('sess-new');
    expect(useAppStore.getState().activeSessionId).toBe('sess-new');
    expect(useAppStore.getState().messages).toHaveLength(1);
  });

  it('clearMessages empties messages array', () => {
    useAppStore.setState({ messages: [makeChatMessage(), makeChatMessage({ id: 'msg-2' })] });
    useAppStore.getState().clearMessages();
    expect(useAppStore.getState().messages).toEqual([]);
  });

  it('selectedProvider is persisted to localStorage; schema is not', () => {
    useAppStore.getState().setSelectedProvider('claude');
    useAppStore.getState().setSchema({ source_type: 'postgresql', tables: [], relationships: [], metadata: {} });

    const raw = localStorage.getItem('savvina-app');
    expect(raw).not.toBeNull();
    const stored = JSON.parse(raw!);
    expect(stored.state.selectedProvider).toBe('claude');
    expect(stored.state.schema).toBeUndefined();
  });
});
