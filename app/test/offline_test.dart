import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:passaly_classes/api.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  setUp(() {
    SharedPreferences.setMockInitialValues({});
    Api.base = 'https://example.test';
    Api.key = 'k';
  });

  test('offline: the last class material is used, questions wait, then go in order', () async {
    var online = true;
    final posted = <String>[];
    Api.client = MockClient((req) async {
      if (!online) throw http.ClientException('no network');
      if (req.method == 'POST') {
        posted.add(jsonDecode(req.body)['question'] as String);
        return http.Response('{}', 201);
      }
      return http.Response(jsonEncode({'lines': ['A parkway was a road through a park.']}), 200);
    });

    expect((await Api.pack(1))['lines'], hasLength(1)); // online: saved on the phone
    online = false;
    expect((await Api.pack(1))['lines'], hasLength(1)); // offline: the saved copy
    expect(() => Api.pack(2), throwsA(isA<http.ClientException>())); // never seen: no copy

    expect(await Api.record(1, 'Why parkway?', 'A road through a park.', true), isFalse);
    expect(await Api.record(1, 'Capital of Australia?', 'Sent to Giovanna.', false), isFalse);
    expect(posted, isEmpty);

    online = true;
    await Api.flushPending();
    expect(posted, ['Why parkway?', 'Capital of Australia?']);
    await Api.flushPending();
    expect(posted, hasLength(2)); // nothing sent twice
  });

  test('a server error is not hidden behind the saved copy', () async {
    var fail = false;
    Api.client = MockClient((req) async =>
        fail ? http.Response('{"detail":"Class not found"}', 404) : http.Response('{"lines":[]}', 200));
    await Api.pack(1);
    fail = true;
    expect(() => Api.pack(1), throwsA(isA<ApiError>()));
  });
}
