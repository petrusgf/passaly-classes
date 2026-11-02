import 'package:flutter/material.dart';

import 'api.dart';
import 'models_screen.dart';
import 'room_screen.dart';

const brand = Color(0xFFEC662E);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final signedIn = await Api.restore();
  runApp(MaterialApp(
    title: 'Passaly Classes',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(colorSchemeSeed: brand, useMaterial3: true),
    home: signedIn ? const HomeScreen() : const StartScreen(),
  ));
}

void toast(BuildContext context, Object message) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('$message')));

class StartScreen extends StatefulWidget {
  const StartScreen({super.key});
  @override
  State<StartScreen> createState() => _StartScreenState();
}

class _StartScreenState extends State<StartScreen> {
  final _server = TextEditingController(
      text: Api.base.isEmpty ? 'passaly.com/classes' : Api.base.replaceFirst(RegExp(r'^https?://'), ''));
  final _name = TextEditingController(text: Api.name);
  bool _busy = false;

  Future<void> _go() async {
    setState(() => _busy = true);
    try {
      await Api.signIn(_server.text, _name.text);
      if (mounted) {
        Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const HomeScreen()));
      }
    } catch (e) {
      if (mounted) toast(context, 'Could not reach the class server: $e');
    }
    if (mounted) setState(() => _busy = false);
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        body: SafeArea(
          child: ListView(padding: const EdgeInsets.all(24), children: [
            const SizedBox(height: 40),
            Text('Passaly', style: Theme.of(context).textTheme.displaySmall?.copyWith(color: brand, fontWeight: FontWeight.bold)),
            const Text('Classes with a personal tutor that runs on your phone.'),
            const SizedBox(height: 32),
            TextField(controller: _name, decoration: const InputDecoration(labelText: 'Your name', border: OutlineInputBorder())),
            const SizedBox(height: 12),
            TextField(
              controller: _server,
              keyboardType: TextInputType.url,
              decoration: const InputDecoration(
                  labelText: 'Class server', hintText: 'passaly.com/classes', border: OutlineInputBorder(),
                  helperText: 'passaly.com/classes, or a laptop on this Wi-Fi'),
            ),
            const SizedBox(height: 24),
            FilledButton(onPressed: _busy ? null : _go, child: Text(_busy ? 'Connecting…' : 'Continue')),
          ]),
        ),
      );
}

class HomeScreen extends StatefulWidget {
  const HomeScreen({super.key});
  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  List<Map<String, dynamic>> _classes = [];
  List<Map<String, dynamic>> _inbox = [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final c = await Api.classes();
      final i = await Api.inbox();
      if (!mounted) return;
      setState(() {
        _classes = c;
        _inbox = i;
      });
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  Future<void> _open(Map<String, dynamic> c) async {
    await Navigator.of(context).push(MaterialPageRoute(builder: (_) => RoomScreen(id: c['id'] as int)));
    _load();
  }

  Future<void> _create() async {
    final title = TextEditingController(), subject = TextEditingController();
    var lang = 'en-US';
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, set) => AlertDialog(
          title: const Text('Start a class'),
          content: Column(mainAxisSize: MainAxisSize.min, children: [
            TextField(controller: title, decoration: const InputDecoration(labelText: 'Title')),
            TextField(controller: subject, decoration: const InputDecoration(labelText: 'Subject')),
            DropdownButtonFormField<String>(
              initialValue: lang,
              decoration: const InputDecoration(labelText: 'Spoken language'),
              items: const [
                DropdownMenuItem(value: 'en-US', child: Text('English (US)')),
                DropdownMenuItem(value: 'en-CA', child: Text('English (Canada)')),
                DropdownMenuItem(value: 'pt-BR', child: Text('Português (Brasil)')),
                DropdownMenuItem(value: 'es-ES', child: Text('Español')),
                DropdownMenuItem(value: 'fr-CA', child: Text('Français (Canada)')),
              ],
              onChanged: (v) => set(() => lang = v!),
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
      _open(await Api.create(title.text, subject.text, lang));
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  Future<void> _join() async {
    final code = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Join a class'),
        content: TextField(controller: code, decoration: const InputDecoration(labelText: 'Invite link or code')),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Join')),
        ],
      ),
    );
    if (ok != true) return;
    try {
      _open(await Api.join(code.text));
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  Future<void> _answer(Map<String, dynamic> q) async {
    final text = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('${q['student']} asks'),
        content: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('${q['question']}', style: const TextStyle(fontWeight: FontWeight.w600)),
          Text('${q['class_title']}', style: Theme.of(ctx).textTheme.bodySmall),
          const SizedBox(height: 12),
          TextField(controller: text, maxLines: 4, decoration: const InputDecoration(
              labelText: 'Your answer', helperText: 'Goes to the student and into the class tutor', border: OutlineInputBorder())),
        ]),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Later')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Send')),
        ],
      ),
    );
    if (ok != true) return;
    try {
      await Api.answer(q['id'] as int, text.text);
      _load();
    } catch (e) {
      if (mounted) toast(context, e);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(
          title: const Text('Passaly Classes'),
          actions: [
            IconButton(
              tooltip: 'Llama on this phone',
              icon: const Icon(Icons.memory),
              onPressed: () => Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ModelsScreen())),
            ),
            PopupMenuButton<String>(
              onSelected: (_) async {
                await Api.signOut();
                if (context.mounted) {
                  Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const StartScreen()));
                }
              },
              itemBuilder: (_) => [PopupMenuItem(value: 'out', child: Text('Sign out (${Api.name})'))],
            ),
          ],
        ),
        floatingActionButton: FloatingActionButton.extended(
            onPressed: _create, icon: const Icon(Icons.add), label: const Text('Start a class')),
        body: RefreshIndicator(
          onRefresh: _load,
          child: ListView(padding: const EdgeInsets.fromLTRB(16, 8, 16, 96), children: [
            OutlinedButton.icon(onPressed: _join, icon: const Icon(Icons.link), label: const Text('Join with an invite')),
            if (_inbox.isNotEmpty) ...[
              const SizedBox(height: 16),
              Text('Questions waiting for you', style: Theme.of(context).textTheme.titleMedium),
              for (final q in _inbox)
                Card(
                  color: Colors.amber.shade50,
                  child: ListTile(
                    leading: const Icon(Icons.help_outline),
                    title: Text('${q['question']}'),
                    subtitle: Text('${q['student']} · ${q['class_title']}'),
                    onTap: () => _answer(q),
                  ),
                ),
            ],
            const SizedBox(height: 16),
            Text('My classes', style: Theme.of(context).textTheme.titleMedium),
            if (_classes.isEmpty) const Padding(padding: EdgeInsets.all(8), child: Text('No classes yet.')),
            for (final c in _classes)
              Card(
                child: ListTile(
                  title: Text('${c['title']}'),
                  subtitle: Text(c['role'] == 'teacher' ? 'You teach' : 'With ${c['teacher']}'),
                  trailing: Chip(
                    label: Text('${c['status']}'),
                    backgroundColor: c['status'] == 'live' ? Colors.red.shade100 : null,
                  ),
                  onTap: () => _open(c),
                ),
              ),
          ]),
        ),
      );
}
