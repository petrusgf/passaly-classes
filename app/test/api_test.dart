import 'package:flutter_test/flutter_test.dart';
import 'package:passaly_classes/api.dart';

void main() {
  test('server address: domains get https, the laptop gets http', () {
    expect(Api.normalize('passaly.com/classes/'), 'https://passaly.com/classes');
    expect(Api.normalize('192.168.1.76:8010'), 'http://192.168.1.76:8010');
    expect(Api.normalize('localhost:8010'), 'http://localhost:8010');
    expect(Api.normalize('http://passaly.com/classes'), 'http://passaly.com/classes');
  });
}
