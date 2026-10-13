package app.mochi.agent;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.Image;
import android.media.ImageReader;
import android.media.MediaRecorder;
import android.os.Handler;
import android.util.Log;
import android.util.Size;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.util.Collections;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** One still from a camera, or a few seconds from the microphone, on request from the desk. */
final class Capture {
    static final String TAG = Store.TAG;

    /** A JPEG (longest side ~1600 px, under ~700 KB) from the back or front camera. Blocks, up to ~10 s. */
    static byte[] photo(Context ctx, boolean front, Handler caller) throws Exception {
        // the camera's callbacks need a thread that is not the one waiting for them
        android.os.HandlerThread ct = new android.os.HandlerThread("mochi-cam");
        ct.start();
        Handler h = new Handler(ct.getLooper());
        try {
            return photoOn(ctx, front, h);
        } finally {
            ct.quitSafely();
        }
    }

    private static byte[] photoOn(Context ctx, boolean front, Handler h) throws Exception {
        CameraManager cm = ctx.getSystemService(CameraManager.class);
        String id = null;
        for (String c : cm.getCameraIdList()) {
            Integer facing = cm.getCameraCharacteristics(c).get(CameraCharacteristics.LENS_FACING);
            if (facing != null && facing == (front ? CameraCharacteristics.LENS_FACING_FRONT : CameraCharacteristics.LENS_FACING_BACK)) { id = c; break; }
        }
        if (id == null) throw new Exception("no " + (front ? "front" : "back") + " camera");
        final String id2 = id;
        StreamConfigurationMap map = cm.getCameraCharacteristics(id).get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        Size best = null;
        for (Size s : map.getOutputSizes(ImageFormat.JPEG))  // the largest ordinary 4:3 frame that is not absurd
            if (s.getWidth() * 3 == s.getHeight() * 4 && s.getWidth() <= 2400 && (best == null || s.getWidth() > best.getWidth())) best = s;
        if (best == null) best = map.getOutputSizes(ImageFormat.JPEG)[0];
        ImageReader reader = ImageReader.newInstance(best.getWidth(), best.getHeight(), ImageFormat.JPEG, 2);
        // a small YUV stream runs first so exposure and focus settle; a JPEG-only repeating stream never delivers on some HALs
        Size small = null;
        for (Size sz : map.getOutputSizes(ImageFormat.YUV_420_888))
            if (sz.getWidth() <= 800 && (small == null || sz.getWidth() > small.getWidth())) small = sz;
        if (small == null) small = map.getOutputSizes(ImageFormat.YUV_420_888)[0];
        ImageReader warm = ImageReader.newInstance(small.getWidth(), small.getHeight(), ImageFormat.YUV_420_888, 3);
        warm.setOnImageAvailableListener(r -> { try (Image img = r.acquireLatestImage()) { } }, h);
        AtomicReference<byte[]> out = new AtomicReference<>();
        AtomicReference<Exception> err = new AtomicReference<>();
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<CameraDevice> dev = new AtomicReference<>();
        reader.setOnImageAvailableListener(r -> {
            try (Image img = r.acquireLatestImage()) {
                if (img == null || out.get() != null) return;
                ByteBuffer b = img.getPlanes()[0].getBuffer();
                byte[] bytes = new byte[b.remaining()];
                b.get(bytes);
                out.set(bytes);
                done.countDown();
            }
        }, h);
        cm.openCamera(id, new CameraDevice.StateCallback() {
            @Override public void onOpened(CameraDevice d) {
                dev.set(d);
                try {
                    d.createCaptureSession(java.util.Arrays.asList(warm.getSurface(), reader.getSurface()), new CameraCaptureSession.StateCallback() {
                        @Override public void onConfigured(CameraCaptureSession s) {
                            try {
                                CaptureRequest.Builder pv = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                                pv.addTarget(warm.getSurface());
                                pv.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
                                pv.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
                                s.setRepeatingRequest(pv.build(), null, h);
                                h.postDelayed(() -> {
                                    try {
                                        CaptureRequest.Builder rq = d.createCaptureRequest(CameraDevice.TEMPLATE_STILL_CAPTURE);
                                        rq.addTarget(reader.getSurface());
                                        rq.set(CaptureRequest.CONTROL_MODE, CaptureRequest.CONTROL_MODE_AUTO);
                                        rq.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE);
                                        rq.set(CaptureRequest.JPEG_QUALITY, (byte) 85);
                                        rq.set(CaptureRequest.JPEG_ORIENTATION, jpegOrientation(cm, id2, front));
                                        s.capture(rq.build(), null, h);
                                    } catch (Exception e) { err.set(e); done.countDown(); }
                                }, 1500);
                            } catch (Exception e) { err.set(e); done.countDown(); }
                        }
                        @Override public void onConfigureFailed(CameraCaptureSession s) { err.set(new Exception("camera session failed")); done.countDown(); }
                    }, h);
                } catch (Exception e) { err.set(e); done.countDown(); }
            }
            @Override public void onDisconnected(CameraDevice d) { err.set(new Exception("camera disconnected")); done.countDown(); }
            @Override public void onError(CameraDevice d, int error) { err.set(new Exception("camera error " + error)); done.countDown(); }
        }, h);
        try {
            if (!done.await(15, TimeUnit.SECONDS)) throw new Exception("camera timed out");
            if (err.get() != null) throw err.get();
            return shrink(out.get(), 1600, 700 * 1024);
        } finally {
            CameraDevice d = dev.get();
            if (d != null) d.close();
            reader.close();
            warm.close();
        }
    }

    /** Upright pictures: the sensor's orientation, for a phone held upright. */
    static int jpegOrientation(CameraManager cm, String id, boolean front) throws Exception {
        Integer so = cm.getCameraCharacteristics(id).get(CameraCharacteristics.SENSOR_ORIENTATION);
        return so == null ? 0 : so;
    }

    /** Re-encode until it fits: the phone's JPEG is many MB, the relay wants something that travels. */
    static byte[] shrink(byte[] jpeg, int maxSide, int maxBytes) {
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length, o);
        int sample = 1;
        while (Math.max(o.outWidth, o.outHeight) / sample > maxSide) sample *= 2;
        BitmapFactory.Options o2 = new BitmapFactory.Options();
        o2.inSampleSize = sample;
        Bitmap bm = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length, o2);
        if (bm == null) return jpeg;
        for (int q = 85; q >= 40; q -= 15) {
            ByteArrayOutputStream bo = new ByteArrayOutputStream();
            bm.compress(Bitmap.CompressFormat.JPEG, q, bo);
            if (bo.size() <= maxBytes || q == 40) return bo.toByteArray();
        }
        return jpeg;
    }

    /** AAC in an .m4a, mono 16 kHz 32 kbps: ~4 KB per second. Blocks for the duration. */
    static byte[] audio(Context ctx, int seconds) throws Exception {
        File f = File.createTempFile("mochi-audio", ".m4a", ctx.getCacheDir());
        MediaRecorder r = new MediaRecorder(ctx);
        try {
            r.setAudioSource(MediaRecorder.AudioSource.MIC);
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4);
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC);
            r.setAudioChannels(1);
            r.setAudioSamplingRate(16000);
            r.setAudioEncodingBitRate(32000);
            r.setOutputFile(f.getAbsolutePath());
            r.prepare();
            r.start();
            Thread.sleep(Math.max(1, Math.min(seconds, 120)) * 1000L);
            r.stop();
            return Files.readAllBytes(f.toPath());
        } finally {
            r.release();
            if (!f.delete()) Log.w(TAG, "temp audio not deleted");
        }
    }
}
