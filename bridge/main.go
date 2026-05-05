package main

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/mdp/qrterminal"
	_ "modernc.org/sqlite"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

var processorURL string
var storeDir string

// MessageStore holds media metadata needed for download and replay.
type MessageStore struct {
	db *sql.DB
}

func NewMessageStore(dir string) (*MessageStore, error) {
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}
	db, err := sql.Open("sqlite", fmt.Sprintf("file:%s/messages.db?_pragma=foreign_keys(1)", dir))
	if err != nil {
		return nil, err
	}
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			last_message_time TIMESTAMP
		);
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);
	`)
	if err != nil {
		db.Close()
		return nil, err
	}
	return &MessageStore{db: db}, nil
}

func (s *MessageStore) Close() error { return s.db.Close() }

func (s *MessageStore) StoreChat(jid string, t time.Time) error {
	_, err := s.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, last_message_time) VALUES (?, ?)", jid, t,
	)
	return err
}

func (s *MessageStore) StoreMessage(id, chatJID, sender string, t time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	if mediaType == "" {
		return nil
	}
	_, err := s.db.Exec(
		`INSERT OR REPLACE INTO messages
		(id, chat_jid, sender, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, t, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
	)
	return err
}

func (s *MessageStore) GetMediaInfo(id, chatJID string) (mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64, err error) {
	err = s.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)
	return
}

func extractMediaInfo(msg *waProto.Message) (mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return
	}
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "audio_" + time.Now().Format("20060102_150405") + ".ogg",
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}
	if img := msg.GetImageMessage(); img != nil {
		return "image", "image_" + time.Now().Format("20060102_150405") + ".jpg",
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", "video_" + time.Now().Format("20060102_150405") + ".mp4",
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}
	return
}

// MediaDownloader implements whatsmeow.DownloadableMessage.
type MediaDownloader struct {
	URL, DirectPath                      string
	MediaKey, FileSHA256, FileEncSHA256  []byte
	FileLength                           uint64
	MediaType                            whatsmeow.MediaType
}

func (d *MediaDownloader) GetDirectPath() string          { return d.DirectPath }
func (d *MediaDownloader) GetURL() string                 { return d.URL }
func (d *MediaDownloader) GetMediaKey() []byte            { return d.MediaKey }
func (d *MediaDownloader) GetFileLength() uint64          { return d.FileLength }
func (d *MediaDownloader) GetFileSHA256() []byte          { return d.FileSHA256 }
func (d *MediaDownloader) GetFileEncSHA256() []byte       { return d.FileEncSHA256 }
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType { return d.MediaType }

func downloadMedia(client *whatsmeow.Client, store *MessageStore, messageID, chatJID string) (string, error) {
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err := store.GetMediaInfo(messageID, chatJID)
	if err != nil {
		return "", fmt.Errorf("message not found: %v", err)
	}
	if mediaType == "" {
		return "", fmt.Errorf("not a media message")
	}

	chatDir := filepath.Join(storeDir, strings.ReplaceAll(chatJID, ":", "_"))
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return "", err
	}
	localPath := filepath.Join(chatDir, filename)
	absPath, _ := filepath.Abs(localPath)

	if _, err := os.Stat(localPath); err == nil {
		return absPath, nil
	}

	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return "", fmt.Errorf("incomplete media info for download")
	}

	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	default:
		return "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	directPath := extractDirectPathFromURL(url)
	dl := &MediaDownloader{
		URL: url, DirectPath: directPath, MediaKey: mediaKey,
		FileLength: fileLength, FileSHA256: fileSHA256, FileEncSHA256: fileEncSHA256,
		MediaType: waMediaType,
	}

	data, err := client.Download(context.Background(), dl)
	if err != nil {
		return "", fmt.Errorf("download failed: %v", err)
	}
	if err := os.WriteFile(localPath, data, 0644); err != nil {
		return "", err
	}
	fmt.Printf("[bridge] downloaded %s to %s\n", mediaType, absPath)
	return absPath, nil
}

func extractDirectPathFromURL(url string) string {
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url
	}
	return "/" + strings.SplitN(parts[1], "?", 2)[0]
}

func sendAudio(client *whatsmeow.Client, recipientJID types.JID, audioPath string) error {
	data, err := os.ReadFile(audioPath)
	if err != nil {
		return err
	}
	resp, err := client.Upload(context.Background(), data, whatsmeow.MediaAudio)
	if err != nil {
		return fmt.Errorf("upload failed: %v", err)
	}

	seconds, waveform, err := analyzeOggOpus(data)
	if err != nil {
		seconds = 5
		waveform = placeholderWaveform(seconds)
	}

	msg := &waProto.Message{
		AudioMessage: &waProto.AudioMessage{
			Mimetype:      proto.String("audio/ogg; codecs=opus"),
			URL:           &resp.URL,
			DirectPath:    &resp.DirectPath,
			MediaKey:      resp.MediaKey,
			FileEncSHA256: resp.FileEncSHA256,
			FileSHA256:    resp.FileSHA256,
			FileLength:    &resp.FileLength,
			Seconds:       proto.Uint32(seconds),
			PTT:           proto.Bool(true),
			Waveform:      waveform,
		},
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	_, err = client.SendMessage(ctx, recipientJID, msg)
	return err
}

func sendText(client *whatsmeow.Client, jid types.JID, text string) {
	msg := &waProto.Message{Conversation: proto.String(text)}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if _, err := client.SendMessage(ctx, jid, msg); err != nil {
		fmt.Printf("[bridge] sendText failed: %v\n", err)
	}
}

func handleVMCommand(client *whatsmeow.Client, selfJID types.JID, text string) {
	parts := strings.Fields(strings.TrimSpace(text))
	if len(parts) < 2 {
		sendText(client, selfJID, "usage: !vm reverb | beat <name> | snap <0-1> | gain <0-1> | status")
		return
	}

	var update map[string]interface{}
	var reply string

	switch parts[1] {
	case "reverb":
		update = map[string]interface{}{"mode": "reverb"}
		reply = "mode: reverb ✓"

	case "beat":
		if len(parts) < 3 {
			sendText(client, selfJID, "usage: !vm beat <the_ghetto|gin_juice>")
			return
		}
		update = map[string]interface{}{"mode": "beat", "beat": parts[2]}
		reply = fmt.Sprintf("mode: beat — %s ✓", parts[2])

	case "snap":
		if len(parts) < 3 {
			sendText(client, selfJID, "usage: !vm snap <0.0-1.0>")
			return
		}
		val, err := strconv.ParseFloat(parts[2], 64)
		if err != nil || val < 0 || val > 1 {
			sendText(client, selfJID, "snap must be between 0.0 and 1.0")
			return
		}
		update = map[string]interface{}{"snap_strength": val}
		reply = fmt.Sprintf("snap: %.2f ✓", val)

	case "gain":
		if len(parts) < 3 {
			sendText(client, selfJID, "usage: !vm gain <0.0-1.0>")
			return
		}
		val, err := strconv.ParseFloat(parts[2], 64)
		if err != nil || val < 0 || val > 1 {
			sendText(client, selfJID, "gain must be between 0.0 and 1.0")
			return
		}
		update = map[string]interface{}{"gain": val}
		reply = fmt.Sprintf("gain: %.2f ✓", val)

	case "status":
		resp, err := http.Get(processorURL + "/config")
		if err != nil {
			sendText(client, selfJID, fmt.Sprintf("error reaching processor: %v", err))
			return
		}
		defer resp.Body.Close()
		var cfg map[string]interface{}
		json.NewDecoder(resp.Body).Decode(&cfg)
		sendText(client, selfJID, fmt.Sprintf("mode=%v beat=%v snap=%v gain=%v",
			cfg["mode"], cfg["beat"], cfg["snap_strength"], cfg["gain"]))
		return

	default:
		sendText(client, selfJID, "unknown command — try: reverb | beat <name> | snap <val> | gain <val> | status")
		return
	}

	data, _ := json.Marshal(update)
	resp, err := http.Post(processorURL+"/config", "application/json", bytes.NewReader(data))
	if err != nil {
		sendText(client, selfJID, fmt.Sprintf("error reaching processor: %v", err))
		return
	}
	resp.Body.Close()
	sendText(client, selfJID, reply)
}

func handleMessage(client *whatsmeow.Client, store *MessageStore, msg *events.Message) {
	chatJID := msg.Info.Chat.String()
	store.StoreChat(chatJID, msg.Info.Timestamp)

	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)
	if mediaType != "" {
		if err := store.StoreMessage(
			msg.Info.ID, chatJID, msg.Info.Sender.User, msg.Info.Timestamp, msg.Info.IsFromMe,
			mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
		); err != nil {
			fmt.Printf("[bridge] store message error: %v\n", err)
		}
	}

	if msg.Info.IsFromMe {
		if aud := msg.Message.GetAudioMessage(); aud != nil && aud.GetPTT() {
			fmt.Printf("[bridge] outgoing voice note %s → dispatching to processor\n", msg.Info.ID)
			go dispatchToProcessor(msg.Info.ID, chatJID)
		}

		// !vm commands sent to self
		text := msg.Message.GetConversation()
		if text == "" {
			if ext := msg.Message.GetExtendedTextMessage(); ext != nil {
				text = ext.GetText()
			}
		}
		if text != "" {
			fmt.Printf("[bridge] outgoing text — chat=%s myID=%v text=%q\n",
				msg.Info.Chat.String(), client.Store.ID, text)
		}
		if strings.HasPrefix(text, "!vm") {
			isSelf := client.Store.ID != nil && (msg.Info.Chat.User == client.Store.ID.User ||
				msg.Info.Chat.String() == client.Store.ID.ToNonAD().String())
			if isSelf {
				go handleVMCommand(client, msg.Info.Chat, text)
			} else {
				fmt.Printf("[bridge] !vm ignored — chat %s is not self (%v)\n",
					msg.Info.Chat.String(), client.Store.ID)
			}
		}
	}
}

func dispatchToProcessor(messageID, chatJID string) {
	payload := map[string]string{"message_id": messageID, "chat_jid": chatJID}
	data, _ := json.Marshal(payload)
	resp, err := http.Post(processorURL+"/process", "application/json", bytes.NewReader(data))
	if err != nil {
		fmt.Printf("[processor] dispatch failed: %v\n", err)
		return
	}
	resp.Body.Close()
}

func startRESTServer(client *whatsmeow.Client, store *MessageStore, port int) {
	mux := http.NewServeMux()

	mux.HandleFunc("/api/download", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			MessageID string `json:"message_id"`
			ChatJID   string `json:"chat_jid"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "invalid request", http.StatusBadRequest)
			return
		}
		path, err := downloadMedia(client, store, req.MessageID, req.ChatJID)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "error": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "path": path})
	})

	mux.HandleFunc("/api/revoke", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			MessageID string `json:"message_id"`
			ChatJID   string `json:"chat_jid"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "invalid request", http.StatusBadRequest)
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			http.Error(w, "invalid JID", http.StatusBadRequest)
			return
		}
		revokeMsg := client.BuildRevoke(chatJID, types.EmptyJID, req.MessageID)
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		_, err = client.SendMessage(ctx, chatJID, revokeMsg)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "error": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"success": true})
	})

	mux.HandleFunc("/api/send", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req struct {
			Recipient string `json:"recipient"`
			AudioPath string `json:"audio_path"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Recipient == "" || req.AudioPath == "" {
			http.Error(w, "invalid request", http.StatusBadRequest)
			return
		}
		recipientJID, err := types.ParseJID(req.Recipient)
		if err != nil {
			recipientJID = types.JID{User: req.Recipient, Server: "s.whatsapp.net"}
		}
		err = sendAudio(client, recipientJID, req.AudioPath)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "error": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(map[string]interface{}{"success": true})
	})

	mux.HandleFunc("/api/status", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"connected": client.IsConnected(),
			"logged_in": client.IsLoggedIn(),
		})
	})

	go func() {
		addr := fmt.Sprintf(":%d", port)
		fmt.Printf("[bridge] REST API listening on %s\n", addr)
		if err := http.ListenAndServe(addr, mux); err != nil {
			fmt.Printf("[bridge] server error: %v\n", err)
		}
	}()
}

func main() {
	port := flag.Int("port", 8080, "bridge HTTP port")
	storeDirFlag := flag.String("store-dir", "store", "session and media storage directory")
	processorFlag := flag.String("processor-url", "http://localhost:9000", "voice processor service URL")
	flag.Parse()

	storeDir = *storeDirFlag
	processorURL = *processorFlag

	logger := waLog.Stdout("bridge", "INFO", true)

	if err := os.MkdirAll(storeDir, 0755); err != nil {
		logger.Errorf("failed to create store dir: %v", err)
		return
	}

	dbLog := waLog.Stdout("db", "ERROR", true)
	container, err := sqlstore.New(context.Background(), "sqlite",
		fmt.Sprintf("file:%s/whatsapp.db?_pragma=foreign_keys(1)", storeDir), dbLog)
	if err != nil {
		logger.Errorf("failed to open whatsapp db: %v", err)
		return
	}

	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			deviceStore = container.NewDevice()
		} else {
			logger.Errorf("failed to get device: %v", err)
			return
		}
	}

	client := whatsmeow.NewClient(deviceStore, logger)

	msgStore, err := NewMessageStore(storeDir)
	if err != nil {
		logger.Errorf("failed to init message store: %v", err)
		return
	}
	defer msgStore.Close()

	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			handleMessage(client, msgStore, v)
		case *events.Connected:
			logger.Infof("connected to WhatsApp")
		case *events.LoggedOut:
			logger.Warnf("logged out — rescan QR to reconnect")
		}
	})

	if client.Store.ID == nil {
		qrChan, _ := client.GetQRChannel(context.Background())
		if err := client.Connect(); err != nil {
			logger.Errorf("connect failed: %v", err)
			return
		}
		for evt := range qrChan {
			if evt.Event == "code" {
				fmt.Println("\nScan this QR code with WhatsApp:")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
			} else if evt.Event == "success" {
				break
			}
		}
	} else {
		if err := client.Connect(); err != nil {
			logger.Errorf("connect failed: %v", err)
			return
		}
	}

	time.Sleep(2 * time.Second)
	if !client.IsConnected() {
		logger.Errorf("failed to establish stable connection")
		return
	}

	fmt.Println("\n[bridge] connected — monitoring outgoing voice notes")
	startRESTServer(client, msgStore, *port)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop

	fmt.Println("[bridge] shutting down")
	client.Disconnect()
}

// analyzeOggOpus extracts duration and generates a waveform from raw Ogg Opus bytes.
func analyzeOggOpus(data []byte) (uint32, []byte, error) {
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file")
	}

	var lastGranule uint64
	var sampleRate uint32 = 48000
	var preSkip uint16
	var foundOpusHead bool

	for i := 0; i < len(data); {
		if i+27 >= len(data) {
			break
		}
		if string(data[i:i+4]) != "OggS" {
			i++
			continue
		}

		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]
		pageSize := 27 + numSegments
		for _, s := range segmentTable {
			pageSize += int(s)
		}

		if !foundOpusHead && pageSeqNum <= 1 {
			pageData := data[i : i+pageSize]
			if hp := bytes.Index(pageData, []byte("OpusHead")); hp >= 0 && hp+16 < len(pageData) {
				hp += 8
				preSkip = binary.LittleEndian.Uint16(pageData[hp+10 : hp+12])
				sampleRate = binary.LittleEndian.Uint32(pageData[hp+12 : hp+16])
				foundOpusHead = true
			}
		}
		if granulePos != 0 {
			lastGranule = granulePos
		}
		i += pageSize
	}

	var duration uint32
	if lastGranule > 0 {
		secs := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(secs))
	} else {
		duration = uint32(float64(len(data)) / 2000.0)
	}
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	return duration, placeholderWaveform(duration), nil
}

func placeholderWaveform(duration uint32) []byte {
	const n = 64
	wf := make([]byte, n)
	rand.Seed(int64(duration))
	freq := float64(min(int(duration), 120)) / 30.0
	for i := range wf {
		pos := float64(i) / float64(n)
		v := 35.0*math.Sin(pos*math.Pi*freq*8) + 17.5*math.Sin(pos*math.Pi*freq*16)
		v += (rand.Float64() - 0.5) * 15
		v = v*(0.7+0.3*math.Sin(pos*math.Pi)) + 50
		if v < 0 {
			v = 0
		} else if v > 100 {
			v = 100
		}
		wf[i] = byte(v)
	}
	return wf
}
