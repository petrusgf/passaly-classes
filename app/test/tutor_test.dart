import 'package:flutter_test/flutter_test.dart';
import 'package:passaly_classes/tutor.dart';

void main() {
  const lines = [
    Line('Giovanna', 'teacher', 'A parkway was first a road built through a park.'),
    Line('Siena', 'student', 'And the driveway?'),
    Line('Giovanna', 'teacher', 'A driveway was the private way you drive up to the house.'),
    Line('Siena', 'student', 'So the words kept the old meaning.'),
    Line('Giovanna', 'teacher', 'Exactly. Words are like old roads that kept their name.'),
  ];
  final pool = passages(lines, [(question: 'What is a subjunctive?', answer: 'It expresses wishes.')], 'Giovanna');

  test('terms drop filler and meet plurals', () {
    expect(terms('Why do we drive on parkways?'), {'drive', 'parkwa'});
    expect(terms('Por que estudando estudar?'), {'estuda'});
  });

  test('windows overlap and cover every line, then the teacher answers', () {
    expect(pool.length, 3);
    expect(pool.first, contains('parkway was first'));
    expect(pool[1], contains('Words are like old roads'));
    expect(pool.last, contains('It expresses wishes.'));
  });

  test('a covered question finds its passage; an uncovered one finds nothing', () {
    expect(retrieve('Why is it called a parkway?', pool).first, contains('road built through a park'));
    expect(retrieve('What is photosynthesis?', pool), isEmpty);
    expect(retrieve('the subjunctive again?', pool).first, contains('expresses wishes'));
    expect(retrieve('why?', pool), isEmpty);
  });

  test('replies: the marker, or silence, go to the teacher', () {
    expect(readReply('Because a parkway ran through a park.'),
        (fromClass: true, answer: 'Because a parkway ran through a park.'));
    expect(readReply('NOT_IN_CLASS').fromClass, false);
    expect(readReply('  ').fromClass, false);
  });

  test('profiles see only the newest part of a long class', () {
    final long = List.generate(400, (i) => Line('Giovanna', 'teacher', 'sentence number $i'));
    expect(tail(long, 100).length, 100);
    expect(tail(long, 100), endsWith('sentence number 399'));
  });
}
