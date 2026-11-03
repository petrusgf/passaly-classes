import 'dart:async';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'api.dart';
import 'llama.dart';
import 'main.dart';

/// Choose where the tutor runs (Llama on this phone, or the server's Muse
/// Spark), pick and download the Llama model, and the license notice Meta's
/// Llama 3.2 Community License asks every distributor to show.
class ModelsScreen extends StatefulWidget {
  const ModelsScreen({super.key});
  @override
  State<ModelsScreen> createState() => _ModelsScreenState();
}

class _ModelsScreenState extends State<ModelsScreen> {
  List<LlamaModel> _models = [];
  Set<String> _installed = {};
  String? _active;
  int _ram = 0;
  ({String file, int id})? _pending;
  Map<String, dynamic>? _progress;
  Timer? _timer;
  String? _error;
  String? _cloudModel;  // the server's Muse model, null when it has none

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    try {
      final models = await Llama.catalogue();
      final files = await Llama.installed();
      final ram = await Llama.ramBytes();
      final active = (await Llama.activeFile())?.uri.pathSegments.last;
      final pending = await Llama.pending();
      if (!mounted) return;
      setState(() {
        _models = models;
        _installed = files.map((f) => f.uri.pathSegments.last).toSet();
        _ram = ram;
        _active = active;
        _pending = pending;
      });
      if (pending != null) _watch();
      final info = await Api.tutorInfo();
      if (mounted) setState(() => _cloudModel = info['cloud'] == true ? '${info['model']}' : null);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  void _watch() {
    _timer?.cancel();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) async {
      final p = _pending;
      if (p == null) return;
      final s = await Llama.status(p.id);
      if (!mounted) return;
      setState(() => _progress = s);
      if (s['state'] == 'running' || s['state'] == 'paused') return;
      _timer?.cancel();
      await Llama.clearPending();
      if (s['state'] == 'done') {
        final m = _models.firstWhere((m) => m.file == p.file);
        setState(() => _progress = {'state': 'checking'});
        final ok = await Llama.verify(m);
        if (ok) await Llama.setActive(m);
        if (mounted) toast(context, ok ? '${m.title} is ready' : 'The download was damaged; try again');
      } else if (mounted) {
        toast(context, 'Download failed');
      }
      setState(() {
        _pending = null;
        _progress = null;
      });
      _load();
    });
  }

  Future<void> _download(LlamaModel m) async {
    final id = await Llama.download(m);
    setState(() => _pending = (file: m.file, id: id));
    _watch();
  }

  Future<void> _showLicense() async {
    try {
      final r = await http.get(Uri.parse('${Api.base}/models/LICENSE.txt'));
      if (!mounted) return;
      showDialog(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('Llama 3.2 Community License'),
          content: SingleChildScrollView(child: Text(r.statusCode == 200 ? r.body : 'Not on the server yet.')),
          actions: [TextButton(onPressed: () => Navigator.pop(ctx), child: const Text('Close'))],
        ),
      );
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('Tutor AI')),
        body: ListView(padding: const EdgeInsets.all(16), children: [
          Text('Where the tutor runs', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 8),
          SegmentedButton<String>(
            segments: [
              const ButtonSegment(value: 'llama', icon: Icon(Icons.phone_android), label: Text('This phone · Llama')),
              ButtonSegment(value: 'cloud', icon: const Icon(Icons.cloud_outlined), label: const Text('Meta cloud · Muse'),
                  enabled: _cloudModel != null || Api.engine == 'cloud'),
            ],
            selected: {Api.engine},
            onSelectionChanged: (v) async {
              await Api.setEngine(v.first);
              setState(() {});
            },
          ),
          const SizedBox(height: 8),
          Text(Api.engine == 'cloud'
              ? 'Questions go to the class server, which asks Meta\'s ${_cloudModel ?? 'Muse Spark'}. Needs internet.'
              : 'Questions are answered on this phone by Meta\'s Llama 3.2; works without internet.'
                  '${_cloudModel == null ? ' (This server has no Meta cloud model set up.)' : ''}'),
          const Divider(height: 32),
          Text('Llama on this phone', style: Theme.of(context).textTheme.titleMedium),
          if (_ram > 0)
            Text('This phone: ${(_ram / 1e9).toStringAsFixed(1)} GB RAM',
                style: Theme.of(context).textTheme.bodySmall),
          if (_error != null) Padding(padding: const EdgeInsets.only(top: 12), child: Text(_error!)),
          const SizedBox(height: 12),
          for (final m in _models) _card(m),
          const Divider(height: 32),
          Text('Built with Llama', style: Theme.of(context).textTheme.titleMedium?.copyWith(color: brand)),
          const SizedBox(height: 4),
          const Text('Llama 3.2 is licensed under the Llama 3.2 Community License, '
              'Copyright © Meta Platforms, Inc. All Rights Reserved.'),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(onPressed: _showLicense, child: const Text('Read the license')),
          ),
        ]),
      );

  Widget _card(LlamaModel m) {
    final installed = _installed.contains(m.file);
    final downloading = _pending?.file == m.file;
    final fits = _ram == 0 || _ram >= m.minRam;
    Widget action;
    if (downloading) {
      final p = _progress;
      final total = (p?['total'] as int?) ?? 0;
      final done = (p?['downloaded'] as int?) ?? 0;
      action = Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        LinearProgressIndicator(value: total > 0 ? done / total : null),
        Text(p?['state'] == 'checking' ? 'Checking the file…' : 'Downloading ${total > 0 ? '${(100 * done / total).round()}%' : '…'}'),
      ]);
    } else if (installed) {
      action = Row(children: [
        if (_active == m.file)
          const Chip(label: Text('In use'))
        else
          FilledButton(onPressed: () async { await Llama.setActive(m); _load(); }, child: const Text('Use this')),
        const SizedBox(width: 8),
        TextButton(onPressed: () async { await Llama.remove(m); _load(); }, child: const Text('Remove')),
      ]);
    } else {
      action = FilledButton(
        onPressed: fits && _pending == null ? () => _download(m) : null,
        child: Text(fits ? 'Download ${m.size}' : 'Needs more RAM than this phone has'),
      );
    }
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(m.title, style: Theme.of(context).textTheme.titleMedium),
          Text('${m.size} · ${m.note}'),
          const SizedBox(height: 8),
          action,
        ]),
      ),
    );
  }
}
