class AnkerVideoPlayer {
    constructor(video) {
        this.video = video;
        this.queue = [];
        this.buffering = true;
        this.muxer = new JMuxer({
            node: video,
            mode: "video",
            flushingTime: 0,
            fps: 15,
            onError: (data) => console.warn("JMuxer video error", data),
        });
        this.pumpTimer = setInterval(() => this.pump(), 16);
    }

    enqueue(data) {
        if (!(data instanceof ArrayBuffer) || !data.byteLength) return;

        this.queue.push({ data, receivedAt: performance.now() });
        if (this.queue.length > 120) {
            this.queue.splice(0, this.queue.length - 24);
            this.buffering = true;
        }
    }

    pump() {
        const now = performance.now();
        const firstPacket = this.queue[0];
        if (!firstPacket || (this.buffering && this.queue.length < 2 && now - firstPacket.receivedAt < 120)) {
            return;
        }
        this.buffering = false;

        for (let fed = 0; fed < 24 && this.queue.length; fed++) {
            const packet = this.queue[0];
            if (now - packet.receivedAt < 120 && this.queue.length <= 2) break;

            this.queue.shift();
            try {
                this.muxer.feed({ video: new Uint8Array(packet.data) });
            } catch (err) {
                console.warn("Could not feed video packet", err);
                this.buffering = true;
                return;
            }
        }
        this.buffering = !this.queue.length;
    }

    destroy() {
        clearInterval(this.pumpTimer);
        this.muxer.destroy();
        this.video.removeAttribute("src");
        this.video.load();
    }
}
