package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"io"
	"log"
	"os"
	"strings"

	"golang.org/x/crypto/sha3"

	"gomatrixlib/cuckoo"
	"gomatrixlib/fndsa512"
)

const (
	keygenSeedASCII = "msc45xx-fndsa-keygen-seed-v1"
	signSeedASCII   = "msc45xx-fndsa-sign-seed-v1"
	messageASCII    = "matrix federation post-quantum test vector"

	keyIDContextASCII = "matrix:fn-dsa-512:key-id:v1"
)

type shakeReader struct {
	s sha3.ShakeHash
}

func newShakeReader(seed string) io.Reader {
	h := sha3.NewShake256()
	_, _ = h.Write([]byte(seed))
	return &shakeReader{s: h}
}

func (r *shakeReader) Read(p []byte) (int, error) {
	return r.s.Read(p)
}

func b64StdNoPad(b []byte) string {
	return base64.StdEncoding.WithPadding(base64.NoPadding).EncodeToString(b)
}

func b64URLNoPad(b []byte) string {
	return base64.RawURLEncoding.EncodeToString(b)
}

func keyIDDigest(pub []byte) [32]byte {
	ctx := []byte(keyIDContextASCII)
	var len16 [2]byte
	binary.BigEndian.PutUint16(len16[:], uint16(len(ctx)))
	buf := make([]byte, 0, 2+len(ctx)+len(pub))
	buf = append(buf, len16[:]...)
	buf = append(buf, ctx...)
	buf = append(buf, pub...)
	return sha256.Sum256(buf)
}

func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}

func main() {
	keyRng := newShakeReader(keygenSeedASCII)
	signRng := newShakeReader(signSeedASCII)

	priv, pub, err := fndsa512.GenerateKey(keyRng)
	must(err)

	msg := []byte(messageASCII)
	sig, err := fndsa512.Sign(signRng, priv, msg)
	must(err)

	keyDigest := keyIDDigest(pub)
	shortID := b64URLNoPad(keyDigest[:])[:16]

	if len(priv) != fndsa512.PrivateKeySize {
		log.Fatalf("unexpected private key size: %d", len(priv))
	}
	if len(pub) != fndsa512.PublicKeySize {
		log.Fatalf("unexpected public key size: %d", len(pub))
	}
	if len(sig) != fndsa512.SignatureSize {
		log.Fatalf("unexpected signature size: %d", len(sig))
	}

	fmt.Println("[fn-dsa-512]")
	fmt.Println("private_key_base64 =", b64StdNoPad(priv))
	fmt.Println("public_key_base64 =", b64StdNoPad(pub))
	fmt.Println("signature_base64 =", b64StdNoPad(sig))
	fmt.Println("key_id_context_ascii =", keyIDContextASCII)
	fmt.Println("key_id_context_len16_be =", "001b")
	fmt.Println("key_id_sha256_hex =", hex.EncodeToString(keyDigest[:]))
	fmt.Println("key_id_sha256_base64url =", b64URLNoPad(keyDigest[:]))
	fmt.Println("short_id =", shortID)
	fmt.Println()

	fmt.Println("[cuckoo]")
	cuckooSeed := cuckoo.GraphSeed([]byte("challenge"), 3)
	fmt.Println("graph_seed_challenge =", "challenge")
	fmt.Println("graph_seed_nonce =", 3)
	fmt.Println("graph_seed_hex =", hex.EncodeToString(cuckooSeed[:]))

	reducedCfg := cuckoo.Config{EdgeBits: 12, ProofSize: 4}
	reducedSeed := cuckoo.GraphSeed([]byte("tiny-cuckoo-test"), 0)
	proof, err := cuckoo.FindProof(reducedCfg, reducedSeed[:], 1<<12)
	must(err)
	fmt.Println("reduced_config_edge_bits =", reducedCfg.EdgeBits)
	fmt.Println("reduced_config_proof_size =", reducedCfg.ProofSize)
	fmt.Println("reduced_graph_seed_hex =", hex.EncodeToString(reducedSeed[:]))
	fmt.Println("reduced_proof =", proof)
	for _, nonce := range proof {
		edge, err := cuckoo.EdgeForNonce(reducedCfg, reducedSeed[:], nonce)
		must(err)
		fmt.Printf("edge_%d = (%d, %d)\n", nonce, edge.U, edge.V)
	}
	fmt.Println()

	fmt.Println("[pow]")
	stampJSON := strings.Join([]string{
		"{\"algorithm\":\"tk.nutra.msc45xx.pow.cuckoo-cycle-42-29-sha256\",",
		"\"resource\":{\"action\":\"fn-dsa-key-publication\",",
		"\"key_id_sha256\":\"" + b64URLNoPad(keyDigest[:]) + "\",",
		"\"server_name\":\"example.com\"}}",
	}, "")
	powSeed := cuckoo.GraphSeed([]byte(stampJSON), 8137226)
	fmt.Println("stamp_json =", stampJSON)
	fmt.Println("pow_nonce =", 8137226)
	fmt.Println("pow_graph_seed_hex =", hex.EncodeToString(powSeed[:]))

	if !fndsa512.Verify(pub, msg, sig) {
		log.Fatal("generated signature failed to verify")
	}
	if fndsa512.Verify(pub, []byte("tampered"), sig) {
		log.Fatal("tampered message unexpectedly verified")
	}

	_ = os.Stdout.Sync()
}
