package app.mochi.agent;

import android.util.Base64;
import android.util.Log;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * The worst-case path: when the relay has been out of reach for a while, the same sealed blob goes as a mail
 * attachment through Gmail's SMTP (implicit TLS on 465, AUTH PLAIN with an app password) to an address the desk's
 * mbsync pulls. Google sees ciphertext. Config: "smtp": {"host", "port", "user", "pass", "to"} in config.json.
 */
final class Mailer {
    static final String TAG = Store.TAG;

    static void send(String host, int port, String user, String pass, String to, String subject, byte[] blob) throws Exception {
        try (SSLSocket s = (SSLSocket) SSLSocketFactory.getDefault().createSocket(host, port)) {
            s.setSoTimeout(30000);
            s.startHandshake();
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.US_ASCII));
            OutputStream out = s.getOutputStream();
            expect(in, "220");
            cmd(out, in, "EHLO mochi.local", "250");
            String auth = Base64.encodeToString(("\0" + user + "\0" + pass).getBytes(StandardCharsets.UTF_8), Base64.NO_WRAP);
            cmd(out, in, "AUTH PLAIN " + auth, "235");
            cmd(out, in, "MAIL FROM:<" + user + ">", "250");
            cmd(out, in, "RCPT TO:<" + to + ">", "250");
            cmd(out, in, "DATA", "354");
            String boundary = "mochi-" + System.currentTimeMillis();
            StringBuilder m = new StringBuilder();
            m.append("From: Mochi on the phone <").append(user).append(">\r\n")
             .append("To: <").append(to).append(">\r\n")
             .append("Subject: ").append(subject).append("\r\n")
             .append("X-Mochi: phone-fallback\r\n")
             .append("MIME-Version: 1.0\r\n")
             .append("Content-Type: multipart/mixed; boundary=\"").append(boundary).append("\"\r\n\r\n")
             .append("--").append(boundary).append("\r\n")
             .append("Content-Type: text/plain; charset=us-ascii\r\n\r\n")
             .append("Sealed events from the phone; the relay was unreachable. mochi-phone reads this.\r\n\r\n")
             .append("--").append(boundary).append("\r\n")
             .append("Content-Type: application/octet-stream; name=\"mochi.bin\"\r\n")
             .append("Content-Disposition: attachment; filename=\"mochi.bin\"\r\n")
             .append("Content-Transfer-Encoding: base64\r\n\r\n")
             .append(Base64.encodeToString(blob, Base64.CRLF)).append("\r\n")
             .append("--").append(boundary).append("--\r\n.\r\n");
            out.write(m.toString().getBytes(StandardCharsets.US_ASCII));
            out.flush();
            expect(in, "250");
            cmd(out, in, "QUIT", "221");
        }
    }

    private static void cmd(OutputStream out, BufferedReader in, String line, String code) throws Exception {
        out.write((line + "\r\n").getBytes(StandardCharsets.US_ASCII));
        out.flush();
        expect(in, code);
    }

    private static void expect(BufferedReader in, String code) throws Exception {
        String line;
        do {
            line = in.readLine();
            if (line == null) throw new Exception("smtp: connection closed");
        } while (line.length() >= 4 && line.charAt(3) == '-');  // multi-line reply
        if (!line.startsWith(code)) {
            Log.w(TAG, "smtp: " + line);
            throw new Exception("smtp: expected " + code + ", got " + line.substring(0, Math.min(40, line.length())));
        }
    }
}
