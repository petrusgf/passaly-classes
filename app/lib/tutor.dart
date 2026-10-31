/// The class tutor's thinking that is not the model: which parts of the class
/// to show it, how to ask, and how to read the reply.
///
/// A 1B model is a poor judge of "did the class cover this?", so that call is
/// made here first, by matching the question's words against the transcript.
/// No match: the question goes to the teacher without asking the model at
/// all. A match: the model answers from only those passages, small enough for
/// the 4096-token window the phone runs with.
library;

/// Words too common to show that the class talked about something.
/// ponytail: a short English + Portuguese list; a real stemmer if recall suffers.
const _stop = {
  // English
  'about', 'after', 'again', 'also', 'because', 'been', 'before', 'being', 'could',
  'does', 'doing', 'from', 'have', 'here', 'into', 'just', 'like', 'more', 'much',
  'only', 'other', 'over', 'same', 'should', 'some', 'such', 'than', 'that', 'their',
  'them', 'then', 'there', 'these', 'they', 'this', 'very', 'want', 'were', 'what',
  'when', 'where', 'which', 'while', 'with', 'would', 'your', 'yours', 'know',
  'mean', 'means', 'please', 'thanks', 'teacher', 'class', 'today', 'question',
  'explain', 'still', 'dont', 'didnt', 'cant', 'cannot',
  // Portuguese
  'aqui', 'aula', 'como', 'aquele', 'aquela', 'aquilo', 'ainda', 'antes', 'apenas',
  'assim', 'cada', 'coisa', 'com', 'depois', 'desde', 'disso', 'disto', 'entre',
  'essa', 'esse', 'esta', 'este', 'isso', 'isto', 'mais', 'mesmo', 'muito', 'nada',
  'onde', 'outra', 'outro', 'para', 'pela', 'pelo', 'pode', 'porque', 'quando',
  'qual', 'quais', 'quem', 'sobre', 'também', 'tambem', 'tenho', 'todo', 'toda',
  'você', 'voce', 'vocês', 'professora', 'professor', 'pergunta', 'explica',
};

/// Content words, cut to their first six letters so "parkways" meets
/// "parkway" and "estudando" meets "estudar" (crudely, on purpose).
Set<String> terms(String text) => RegExp(r'[\p{L}\p{N}]+', unicode: true)
    .allMatches(text.toLowerCase())
    .map((m) => m.group(0)!.replaceAll("'", ''))
    .where((w) => w.length >= 4 && !_stop.contains(w))
    .map((w) => w.length > 6 ? w.substring(0, 6) : w)
    .toSet();

class Line {
  final String name, role, text;
  const Line(this.name, this.role, this.text);
  factory Line.fromJson(Map<String, dynamic> j) =>
      Line(j['name'] as String, j['role'] as String, j['text'] as String);
  @override
  String toString() => '$name: $text';
}

/// Overlapping windows of the transcript plus the teacher's later answers.
/// Overlap, so an explanation split across two windows is whole in one.
List<String> passages(List<Line> lines, List<({String question, String answer})> answers,
    String teacher, {int size = 4, int step = 2}) {
  final out = <String>[];
  for (var i = 0; i < lines.length; i += step) {
    out.add(lines.skip(i).take(size).join('\n'));
    if (i + size >= lines.length) break;
  }
  for (final a in answers) {
    out.add('Question after class: ${a.question}\n$teacher answered: ${a.answer}');
  }
  return out;
}

/// The best passages for a question, best first; empty when nothing matches.
List<String> retrieve(String question, List<String> pool, {int k = 4, int maxChars = 3000}) {
  final want = terms(question);
  if (want.isEmpty) return const [];
  final scored = [
    for (final p in pool) (p: p, s: terms(p).intersection(want).length),
  ]..removeWhere((x) => x.s == 0);
  scored.sort((a, b) => b.s.compareTo(a.s));
  final out = <String>[];
  var used = 0;
  for (final x in scored.take(k)) {
    if (used + x.p.length > maxChars && out.isNotEmpty) break;
    out.add(x.p);
    used += x.p.length;
  }
  return out;
}

const notInClass = 'NOT_IN_CLASS';

String tutorPrompt({
  required String teacher,
  required String title,
  required String teacherStyle,
  required String studentProfile,
  required List<String> material,
  required String question,
}) =>
    '''You are the after-class tutor for $teacher's class "$title".
Explain the way $teacher does: reuse her examples and words from the class notes. Keep it short and friendly; the student may be a child.
Answer in the language of the question.
Use ONLY the class notes below. If they do not answer the question, reply with only the word $notInClass.

HOW $teacher TEACHES: ${teacherStyle.isEmpty ? 'not known yet' : teacherStyle}
HOW THIS STUDENT LEARNS: ${studentProfile.isEmpty ? 'not known yet' : studentProfile}

CLASS NOTES:
${material.join('\n---\n')}

STUDENT'S QUESTION: $question''';

/// (fromClass, answer). The model saying it is not covered, or saying
/// nothing, sends the question to the teacher.
({bool fromClass, String answer}) readReply(String raw) {
  final t = raw.trim();
  if (t.isEmpty || t.contains(notInClass)) return (fromClass: false, answer: t.replaceAll(notInClass, '').trim());
  return (fromClass: true, answer: t);
}

/// Cut a transcript to the newest [maxChars], for the profile prompts.
String tail(List<Line> lines, int maxChars) {
  final all = lines.join('\n');
  return all.length <= maxChars ? all : all.substring(all.length - maxChars);
}

String teacherStylePrompt(String old, List<Line> lines) =>
    '''Describe in at most 120 words how this teacher teaches: the examples and comparisons she uses, how she checks understanding, pace, tone, and the languages she mixes. Update the current description with what this class shows. Reply with the description only.

CURRENT DESCRIPTION: ${old.isEmpty ? 'none' : old}

CLASS TRANSCRIPT:
${tail(lines, 6000)}''';

String studentProfilePrompt(String old, String name, List<Line> lines, List<String> asked) =>
    '''Describe in at most 100 words how the student $name learns: what confused them, which explanations worked, their level and preferred language. Only learning, no personal details. Update the current description. Reply with the description only.

CURRENT DESCRIPTION: ${old.isEmpty ? 'none' : old}

CLASS TRANSCRIPT:
${tail(lines, 5000)}

THEIR QUESTIONS AFTER CLASS:
${asked.isEmpty ? 'none' : asked.join('\n')}''';
