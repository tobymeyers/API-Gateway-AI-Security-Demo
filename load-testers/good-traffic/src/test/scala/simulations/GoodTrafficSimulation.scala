package simulations

import java.net.URI
import java.net.http.HttpRequest.BodyPublishers
import java.net.http.{HttpClient, HttpRequest, HttpResponse}
import java.nio.charset.StandardCharsets
import java.util.Base64
import java.util.concurrent.atomic.AtomicReference
import java.util.concurrent.{Executors, TimeUnit}

import scala.concurrent.duration._
import scala.util.Random

import io.gatling.core.Predef._
import io.gatling.http.Predef._

/** The "Good Client": steady legitimate traffic with real OAuth M2M tokens.
  *
  * Token refresh runs on one background thread shared by the whole
  * simulation (not per virtual user) so refreshing doesn't itself add
  * load at the target rps - see fetchTokenBlocking below.
  *
  * System properties (pass with -Dname=value):
  *   gateway.url, token.url, client.id, client.secret, table.id,
  *   target.rps (default 20), round.seconds (default 60)
  */
class GoodTrafficSimulation extends Simulation {

  val gatewayBaseUrl: String = sys.props.getOrElse("gateway.url", "https://CHANGE-ME.execute-api.us-east-1.amazonaws.com/prod")
  val tokenUrl: String = sys.props.getOrElse("token.url", "https://CHANGE-ME.auth.us-east-1.amazoncognito.com/oauth2/token")
  val clientId: String = sys.props.getOrElse("client.id", "CHANGE-ME")
  val clientSecret: String = sys.props.getOrElse("client.secret", "CHANGE-ME")
  val tableId: String = sys.props.getOrElse("table.id", "0")
  val targetRps: Int = sys.props.getOrElse("target.rps", "20").toInt
  val roundSeconds: Int = sys.props.getOrElse("round.seconds", "60").toInt
  val scope = "gateway/service1.access gateway/service2.access gateway/service3.access"
  val services: Seq[String] = Seq("service1", "service2", "service3")

  private val httpClient = HttpClient.newHttpClient()

  private def fetchTokenBlocking(): String = {
    val form = s"grant_type=client_credentials&client_id=$clientId&scope=${java.net.URLEncoder.encode(scope, "UTF-8")}"
    val auth = Base64.getEncoder.encodeToString(s"$clientId:$clientSecret".getBytes(StandardCharsets.UTF_8))
    val request = HttpRequest
      .newBuilder()
      .uri(URI.create(tokenUrl))
      .header("Authorization", s"Basic $auth")
      .header("Content-Type", "application/x-www-form-urlencoded")
      .POST(BodyPublishers.ofString(form))
      .build()
    val response = httpClient.send(request, HttpResponse.BodyHandlers.ofString())
    "\"access_token\"\\s*:\\s*\"([^\"]+)\"".r.findFirstMatchIn(response.body()).map(_.group(1)).getOrElse("")
  }

  // Refresh well inside every service's token-freshness baseline (the
  // tightest, Service 3, is 30s) so legitimate traffic never gets denied
  // by staleness alone - see DEPLOYMENT.md review notes for why this
  // cadence and the service baselines have to be chosen together.
  private val tokenRef = new AtomicReference[String](fetchTokenBlocking())
  // Daemon thread so this doesn't keep the JVM alive after sbt's test run finishes.
  private val scheduler = Executors.newSingleThreadScheduledExecutor(r => {
    val t = new Thread(r)
    t.setDaemon(true)
    t
  })
  scheduler.scheduleAtFixedRate(() => tokenRef.set(fetchTokenBlocking()), 15, 15, TimeUnit.SECONDS)

  val httpProtocol = http.baseUrl(gatewayBaseUrl).acceptHeader("application/json")

  // Our own response-body-aware classification, printed per request as a
  // greppable GOOD_REQUEST_RESULT line - parsed back out by
  // scripts/lib/round_report.py's parse_good_traffic_results. Deliberately
  // NOT relying on Gatling's own OK/KO assertion (status in [200,209] or
  // 304) for the good_traffic summary: that check can't distinguish "the
  // agent legitimately denied this" from "an unrelated infra error (e.g.
  // a Lambda concurrency throttle) also returned a non-2xx status" - the
  // exact ambiguity that caused a real misdiagnosis on the ADVERSARIAL
  // side earlier (see replay_attack.py's attack_successful/
  // is_lambda_deny_response). "allowed"/"denied" match only our own
  // deny()/success Lambda response shape (_response in infra/template.yaml,
  // json.dumps(body) - always {"approved": "true"/"false", ...} with a
  // space after the colon, never anything else); everything else
  // (a bare API Gateway error body, a 500, no body at all) is "errored".
  private def classify(body: String): String = {
    if (body.contains("\"approved\": \"true\"")) "allowed"
    else if (body.contains("\"approved\": \"false\"")) "denied"
    else "errored"
  }

  val scn = scenario("Good Load Tester")
    .feed(Iterator.continually(Map("accessToken" -> tokenRef.get(), "service" -> services(Random.nextInt(services.length)))))
    .exec(
      http("Good request to #{service}")
        .get("/#{service}/ping")
        .header("Authorization", "Bearer #{accessToken}")
        .header("X-Expected", "true")
        .header("X-Table-Id", tableId)
        // status.in(...) restores Gatling's own default success check
        // (200-209 or 304), which adding ANY explicit .check() here
        // silently drops otherwise - confirmed live: without this,
        // Gatling's own HTML report showed 480/480 "OK" under a round
        // where the agent genuinely denied every single request (status
        // 403 the whole time) - our own GOOD_REQUEST_RESULT tally below
        // was still correct throughout (it classifies by response BODY
        // content, independent of Gatling's internal pass/fail), but
        // Gatling's native report itself became meaningless the moment
        // status.saveAs/bodyString.optional.saveAs replaced the check
        // Gatling applies automatically when no .check() is present.
        .check(status.in((200 to 209).toList :+ 304: _*))
        .check(status.saveAs("httpStatus"))
        .check(bodyString.optional.saveAs("httpBody"))
    )
    .exec(session => {
      val statusCode = session("httpStatus").asOption[Int].getOrElse(0)
      val body = session("httpBody").asOption[String].getOrElse("")
      println(s"""GOOD_REQUEST_RESULT {"status_code": $statusCode, "outcome": "${classify(body)}"}""")
      session
    })

  setUp(scn.inject(constantUsersPerSec(targetRps).during(roundSeconds.seconds))).protocols(httpProtocol)
}
