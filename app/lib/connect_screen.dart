import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'main.dart';

/// Tokens for AI apps (Claude, LM Studio, Muse Code…) to read this person's
/// classes, meetings and files over MCP. Separate from the login key: shown
/// once, read-only unless allowed, revocable, expiring after 90 days.
class ConnectScreen extends StatefulWidget {
  const ConnectScreen({super.key});
  @override
  State<ConnectScreen> createState() => _ConnectScreenState();
}

class _ConnectScreenState extends State<ConnectScreen> {
  List<Map<String, dynamic>> _tokens = [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final t = await Api.mcpTokens();
      if (mounted) setState(() => _tokens = t);
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  Future<void> _create() async {
    final name = TextEditingController();
    var write = false;
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: const Text('New token'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: name, decoration: const InputDecoration(labelText: 'Which app, e.g. Claude')),
            CheckboxListTile(
              contentPadding: EdgeInsets.zero,
              value: write,
              onChanged: (v) => set(() => write = v ?? false),
              title: const Text('Allow it to add notes and documents'),
            ),
          ]),
          actions: [
            TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
            FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Create')),
          ],
        ),
      ),
    );
    if (ok != true) return;
    try {
      final t = await Api.createMcpToken(name.text, write);
      await _load();
      if (!mounted) return;
      await showDialog(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('Copy it now'),
          content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('It will not be shown again.'),
            const SizedBox(height: 8),
            SelectableText('${t['token']}', style: const TextStyle(fontFamily: 'monospace')),
          ]),
          actions: [
            TextButton(
              onPressed: () => Clipboard.setData(ClipboardData(text: '${t['token']}')),
              child: const Text('Copy'),
            ),
            FilledButton(onPressed: () => Navigator.pop(ctx), child: const Text('Done')),
          ],
        ),
      );
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  Future<void> _revoke(Map<String, dynamic> t) async {
    try {
      await Api.revokeMcpToken(t['id'] as int);
      await _load();
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Connect an AI app')),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: _create, icon: const Icon(Icons.key), label: const Text('New token')),
        body: ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 96), children: [
          const Text('AI apps that support MCP can read your classes, meetings and files. '
              'Give each app its own token; revoke it here at any time.'),
          const SizedBox(height: 12),
          SelectableText('Server: ${Api.base}/mcp\nHeader: Authorization: Bearer <token>',
              style: const TextStyle(fontFamily: 'monospace')),
          const SizedBox(height: 16),
          if (_tokens.isEmpty) const Text('No tokens yet.'),
          for (final t in _tokens)
            Card(
              child: ListTile(
                leading: const Icon(Icons.key),
                title: Text('${t['name']}'),
                subtitle: Text('${t['can_write'] == true ? 'Read and write' : 'Read only'} · '
                    '${t['last_used_at'] == null ? 'never used' : 'used ${'${t['last_used_at']}'.substring(0, 10)}'}'),
                trailing: TextButton(onPressed: () => _revoke(t), child: const Text('Revoke')),
              ),
            ),
        ]),
      );
}
